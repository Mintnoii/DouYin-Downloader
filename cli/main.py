import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from auth import CookieManager
from cli.login_flow import can_interactive_login, interactive_relogin
from cli.progress_display import ProgressDisplay
from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from control.sync_manager import SyncManager
from control.sync_scheduler import SyncScheduler
from core import DouyinAPIClient, DownloaderFactory, LoginRequiredError, URLParser
from storage import Database, FileManager
from utils.logger import set_console_log_level, setup_logger
from utils.notifier import build_notifier
from utils.validators import is_short_url, normalize_short_url

logger = setup_logger("CLI")
display = ProgressDisplay()


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def _run_with_relogin(make_coro, cookie_manager, *, serve=False):
    """Run make_coro(); on LoginRequiredError, relogin once and retry.

    make_coro is a zero-arg callable returning a fresh coroutine each call,
    so the retry re-creates its own DouyinAPIClient with refreshed cookies.
    Refreshed cookies propagate through ``cookie_manager`` as a clean replace
    (not a merge), and both call sites read their cookies from it on retry.
    """
    for attempt in range(2):
        try:
            return await make_coro()
        except LoginRequiredError as exc:
            interactive = can_interactive_login(serve=serve)
            if attempt == 1 or not interactive:
                display.print_error(
                    f"登录态失效，需要重新登录（status {exc.status_code}）："
                    f"{exc.status_msg or '请先登录'}。"
                )
                if not interactive:
                    display.print_warning(
                        "当前为非交互环境，未自动打开浏览器。请手动更新 "
                        "config/cookies.json（或运行 python tools/cookie_fetcher.py 登录）。"
                    )
                raise
            display.print_warning(
                f"检测到未登录（status {exc.status_code}），开始重新登录…"
            )
            new_cookies = await interactive_relogin()
            if not new_cookies:
                display.print_error("重新登录未完成，已中止。")
                raise
            cookie_manager.set_cookies(new_cookies)
            display.print_success("已更新登录态，正在重试…")


async def download_url(
    url: str,
    config: ConfigLoader,
    cookie_manager: CookieManager,
    database: Database = None,
    progress_reporter: ProgressDisplay = None,
):
    if progress_reporter:
        progress_reporter.advance_step("初始化", "创建下载组件")
    file_manager = FileManager(config.get("path"))
    rate_limiter = RateLimiter(max_per_second=float(config.get("rate_limit", 2) or 2))
    retry_handler = RetryHandler(max_retries=config.get("retry_times", 3))
    queue_manager = QueueManager(max_workers=int(config.get("thread", 5) or 5))

    original_url = url

    async with DouyinAPIClient(
        cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        if progress_reporter:
            progress_reporter.advance_step("解析链接", "检查短链并解析 URL")
        # 支持多种短链变体：v.douyin.com / v.iesdouyin.com / 无 scheme 的裸链接
        if is_short_url(url):
            resolved_url = await api_client.resolve_short_url(normalize_short_url(url))
            if resolved_url:
                url = resolved_url
            else:
                if progress_reporter:
                    progress_reporter.update_step("解析链接", "短链解析失败")
                display.print_error(f"Failed to resolve short URL: {url}")
                return None

        parsed = URLParser.parse(url)
        if not parsed:
            if progress_reporter:
                progress_reporter.update_step("解析链接", "URL 解析失败")
            display.print_error(f"Failed to parse URL: {url}")
            return None

        # 当使用 --collect-ids 时，预取收藏夹名称映射以便按名称分目录存储
        raw_collect_ids = config.get("collect_ids")
        if raw_collect_ids and not config.get("collect_map"):
            if isinstance(raw_collect_ids, str):
                collect_ids = [cid.strip() for cid in raw_collect_ids.split(",") if cid.strip()]
            elif isinstance(raw_collect_ids, (list, tuple, set)):
                collect_ids = [str(cid).strip() for cid in raw_collect_ids if str(cid).strip()]
            else:
                collect_ids = []
            if collect_ids:
                if progress_reporter:
                    progress_reporter.advance_step("获取收藏夹信息", "拉取收藏夹名称…")
                collect_map = await _fetch_collect_map(api_client, collect_ids)
                config.update(collect_map=collect_map)

        if not progress_reporter:
            display.print_info(f"URL type: {parsed['type']}")
        if progress_reporter:
            progress_reporter.advance_step("创建下载器", f"URL 类型: {parsed['type']}")

        downloader = DownloaderFactory.create(
            parsed["type"],
            config,
            api_client,
            file_manager,
            cookie_manager,
            database,
            rate_limiter,
            retry_handler,
            queue_manager,
            progress_reporter=progress_reporter,
        )

        if not downloader:
            if progress_reporter:
                progress_reporter.update_step("创建下载器", "未找到匹配下载器")
            display.print_error(f"No downloader found for type: {parsed['type']}")
            return None

        if progress_reporter:
            progress_reporter.advance_step("执行下载", "开始拉取与下载资源")
        try:
            result = await downloader.download(parsed)
        except Exception as exc:
            # Surface fatal downloader errors (e.g. user_info fetch failed
            # because cookies are invalid) as a per-URL failure instead of
            # crashing the whole batch. Keeps multi-URL CLI runs robust while
            # still telling the user why the URL was skipped.
            if progress_reporter:
                progress_reporter.update_step("执行下载", f"失败：{exc}")
            display.print_error(f"Download failed for {url}: {exc}")
            return None

        if progress_reporter:
            progress_reporter.advance_step(
                "记录历史",
                "写入数据库历史" if (result and database) else "数据库未启用，跳过",
            )
        if result and database:
            safe_config = {
                k: v
                for k, v in config.config.items()
                if k not in ("cookies", "cookie", "transcript")
            }
            await database.add_history(
                {
                    "url": original_url,
                    "url_type": parsed["type"],
                    "total_count": result.total,
                    "success_count": result.success,
                    "config": json.dumps(safe_config, ensure_ascii=False),
                }
            )

        if progress_reporter:
            if result:
                progress_reporter.advance_step(
                    "收尾",
                    f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}",
                )
            else:
                progress_reporter.advance_step("收尾", "无可统计结果")

        return result


async def main_async(args):
    if not args.serve and not args.sync:
        display.show_banner()

    if args.config:
        config_path = args.config
    else:
        config_path = "config.yml"

    # 若 config 不存在且使用了 --hot-board / --search / --serve 等独立子命令，
    # 允许以默认配置运行（只要命令行提供了 --path）。
    if not Path(config_path).exists():
        if not (args.hot_board is not None or args.search or args.serve):
            display.print_error(f"Config file not found: {config_path}")
            return
        # For ``--serve`` we still pass the (yet-missing) path so later
        # ``config.save()`` calls from the REST settings endpoint create
        # the file in the right place (e.g. Electron's userData dir).
        # Other subcommands keep the historical behaviour of in-memory
        # defaults.
        if args.serve and args.config:
            config = ConfigLoader(config_path)
        else:
            config = ConfigLoader(None)
    else:
        config = ConfigLoader(config_path)

    if args.path:
        config.update(path=args.path)

    # 独立子命令：热榜 / 搜索 / 服务 / 同步
    if args.hot_board is not None or args.search:
        discovery_cm = CookieManager()
        discovery_cm.set_cookies(config.get_cookies())
        await _run_with_relogin(
            lambda: _run_discovery_subcommand(args, config, discovery_cm),
            discovery_cm,
            serve=False,
        )
        return
    if args.serve:
        await _run_serve_subcommand(args, config)
        return
    if args.sync or args.sync_once:
        await _run_sync_command(args, config)
        return

    if args.list_collections:
        await _run_list_collections(args, config)
        return

    if args.collect:
        ok = await _run_interactive_collect(args, config)
        if not ok:
            return
        # _run_interactive_collect 会设置 config 中的 collect_ids / collect_map
        # 并自动插入 URL，然后继续走下面的下载流程

    if args.url:
        urls = args.url if isinstance(args.url, list) else [args.url]
        for url in urls:
            if url not in config.get("link", []):
                config.update(link=config.get("link", []) + [url])

    if args.thread:
        config.update(thread=args.thread)

    if args.collect_ids:
        config.update(collect_ids=args.collect_ids)
        # 使用 --collect-ids 时自动切换到 collect 模式
        config.update(mode=["collect"])

    if not config.validate():
        display.print_error("Invalid configuration: missing required fields")
        return

    cookies = config.get_cookies()
    cookie_manager = CookieManager()
    cookie_manager.set_cookies(cookies)

    if not cookie_manager.validate_cookies():
        display.print_warning("Cookies may be invalid or incomplete")

    database = None
    if config.get("database"):
        db_path = config.get("database_path", "dy_downloader.db") or "dy_downloader.db"
        database = Database(db_path=str(db_path))
        await database.initialize()
        display.print_success("Database initialized")

    urls = config.get_links()
    display.print_info(f"Found {len(urls)} URL(s) to process")

    all_results = []
    progress_config = config.get("progress", {}) or {}
    quiet_by_config = _as_bool(progress_config.get("quiet_logs", True), default=True)
    quiet_progress_logs = quiet_by_config and not (args.verbose or args.show_warnings)
    if quiet_progress_logs:
        # Progress 运行期间若有大量错误日志会触发 rich 反复重绘，导致屏幕出现重复块。
        # 默认静默控制台日志，下载完成后再恢复。
        set_console_log_level(logging.CRITICAL)

    display.start_download_session(len(urls))
    try:
        for i, url in enumerate(urls, 1):
            display.start_url(i, len(urls), url)

            result = await _run_with_relogin(
                lambda u=url: download_url(
                    u,
                    config,
                    cookie_manager,
                    database,
                    progress_reporter=display,
                ),
                cookie_manager,
                serve=False,
            )
            if result:
                all_results.append(result)
                display.complete_url(result)
            else:
                display.fail_url("下载失败或链接无效")
    finally:
        display.stop_download_session()
        if database is not None:
            await database.close()
        if quiet_progress_logs:
            set_console_log_level(logging.ERROR)

    if all_results:
        from core.downloader_base import DownloadResult

        total_result = DownloadResult()
        for r in all_results:
            total_result.merge(r)

        display.print_success("\n=== Overall Summary ===")
        display.show_result(total_result)

        await _dispatch_notifications(config, total_result, len(urls))
    else:
        # 所有链接都失败时，也发通知（若启用）
        await _dispatch_notifications(config, None, len(urls))


async def _run_discovery_subcommand(
    args, config: ConfigLoader, cookie_manager: CookieManager
) -> None:
    """处理 --hot-board 与 --search 子命令。"""
    from core.discovery import dump_hot_board, search_and_dump

    base_path = Path(config.get("path") or "./Downloaded/")

    async with DouyinAPIClient(cookie_manager.get_cookies()) as api_client:
        if args.hot_board is not None:
            display.print_info("拉取抖音热搜榜...")
            result = await dump_hot_board(api_client, base_path, limit=int(args.hot_board or 0))
            display.print_success(f"热榜已保存：{result['count']} 条 -> {result['path']}")
        if args.search:
            display.print_info(f"搜索关键词：{args.search}")
            result = await search_and_dump(
                api_client,
                args.search,
                base_path,
                max_items=int(args.search_max or 50),
            )
            display.print_success(f"搜索结果已保存：{result['count']} 条 -> {result['path']}")


async def _run_interactive_collect(args, config: ConfigLoader) -> bool:
    """交互式选择收藏夹下载。列出所有收藏夹后提示用户选择。

    Returns ``True`` if the user selected at least one collection and config
    was updated; ``False`` if the user cancelled or an error occurred.
    """
    cookies = config.get_cookies()
    cookie_manager = CookieManager()
    cookie_manager.set_cookies(cookies)

    if not cookie_manager.validate_cookies():
        display.print_warning("Cookie 可能无效或不完整，尝试继续…")

    async with DouyinAPIClient(cookie_manager.get_cookies()) as api_client:
        display.print_info("正在获取收藏夹列表…")
        raw_collects = await _fetch_all_collects(api_client)
        if not raw_collects:
            display.print_warning("未找到任何收藏夹，请确认账号已登录且 cookie 有效")
            return False

        # 按名称排序
        raw_collects.sort(
            key=lambda c: (c.get("collects_name") or c.get("name") or "").lower()
        )
        max_id_len = max(
            (len(str(_extract_collects_id_str(c))) for c in raw_collects),
            default=0,
        )

        display.print_success(f"共 {len(raw_collects)} 个收藏夹：\n")
        for i, item in enumerate(raw_collects, 1):
            cid = _extract_collects_id_str(item)
            cname = (
                item.get("collects_name")
                or item.get("name")
                or item.get("title")
                or "(未命名)"
            )
            count = _extract_collect_count(item)
            display.print_info(
                f"  {i:3d}.  [{cid:<{max_id_len}}]  {cname}  （{count} 个作品）"
            )

        # 读取用户选择
        display.print_info(
            "\n输入要下载的收藏夹编号（多个用逗号分隔，支持范围如 1-5，输入 all 全选）："
        )
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            display.print_warning("\n已取消")
            return False

        if not raw:
            display.print_warning("未选择任何收藏夹，已取消")
            return False

        selected_indices = _parse_collect_selection(raw, len(raw_collects))
        if not selected_indices:
            display.print_warning("选择无效，已取消")
            return False

        # 构建 collect_ids 和 collect_map
        selected_ids: list[str] = []
        collect_map: dict[str, str] = {}
        for idx in selected_indices:
            item = raw_collects[idx]
            cid = _extract_collects_id_str(item)
            cname = (
                item.get("collects_name")
                or item.get("name")
                or item.get("title")
                or cid
            )
            if cid:
                selected_ids.append(cid)
                collect_map[cid] = cname

        if not selected_ids:
            display.print_warning("未能提取有效的收藏夹 ID")
            return False

        display.print_success(
            f"已选择 {len(selected_ids)} 个收藏夹："
            + ", ".join(collect_map.values())
        )

        # 写入 config
        config.update(collect_ids=selected_ids, collect_map=collect_map, mode=["collect"])
        # 自动添加收藏夹 URL（如果用户没通过 -u 指定）
        if not config.get_links():
            config.update(
                link=["https://www.douyin.com/user/self?showTab=favorite_collection"]
            )
        return True


def _parse_collect_selection(raw: str, total: int) -> list[int]:
    """解析用户输入的收藏夹选择字符串，返回 0-based 索引列表。

    支持格式：
    - ``3``          → 第 3 个
    - ``1,3,5``      → 第 1、3、5 个
    - ``1-5``        → 第 1 到 5 个（含两端）
    - ``1,3-5,8``    → 混合
    - ``all`` / ``a`` → 全选
    """
    raw = raw.strip().lower()
    if raw in ("all", "a"):
        return list(range(total))

    indices: set[int] = set()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    for part in parts:
        if "-" in part:
            range_parts = part.split("-", 1)
            try:
                start = int(range_parts[0]) - 1  # 1-based → 0-based
                end = int(range_parts[1])  # inclusive, so no -1
            except (ValueError, IndexError):
                continue
            if start < 0 or end > total or start >= end:
                continue
            indices.update(range(start, end))
        else:
            try:
                idx = int(part) - 1  # 1-based → 0-based
            except ValueError:
                continue
            if 0 <= idx < total:
                indices.add(idx)

    return sorted(indices)


async def _run_list_collections(args, config: ConfigLoader) -> None:
    """列出当前账号所有收藏夹（ID + 名称）。"""
    cookies = config.get_cookies()
    cookie_manager = CookieManager()
    cookie_manager.set_cookies(cookies)

    if not cookie_manager.validate_cookies():
        display.print_warning("Cookie 可能无效或不完整，尝试继续…")

    async with DouyinAPIClient(cookie_manager.get_cookies()) as api_client:
        display.print_info("正在获取收藏夹列表…")
        raw_collects = await _fetch_all_collects(api_client)
        if not raw_collects:
            display.print_warning("未找到任何收藏夹，请确认账号已登录且 cookie 有效")
            return

        display.print_success(f"共 {len(raw_collects)} 个收藏夹：\n")
        # 按收藏夹名称排序（不区分大小写）
        raw_collects.sort(key=lambda c: (c.get("collects_name") or c.get("name") or "").lower())
        max_id_len = max(
            (len(str(_extract_collects_id_str(c))) for c in raw_collects),
            default=0,
        )
        for i, item in enumerate(raw_collects, 1):
            cid = _extract_collects_id_str(item)
            cname = (
                item.get("collects_name")
                or item.get("name")
                or item.get("title")
                or "(未命名)"
            )
            count = _extract_collect_count(item)
            display.print_info(
                f"  {i:3d}.  [{cid:<{max_id_len}}]  {cname}  （{count} 个作品）"
            )

        display.print_info(
            "\n使用 --collect-ids 指定要下载的收藏夹，例如：\n"
            f"  --collect-ids {_extract_collects_id_str(raw_collects[0])}"
            if raw_collects
            else ""
        )


async def _fetch_all_collects(api_client) -> list:
    """分页拉取当前账号的所有收藏夹元数据。"""
    all_items: list = []
    cursor = 0
    has_more = True
    while has_more:
        page = await api_client.get_user_collects("self", max_cursor=cursor, count=20)
        items = page.get("items") or page.get("collects_list") or []
        if not items:
            break
        all_items.extend(items)
        has_more = bool(page.get("has_more", False))
        next_cursor = int(page.get("max_cursor", 0) or 0)
        if has_more and next_cursor == cursor:
            break
        cursor = next_cursor
    return all_items


def _extract_collects_id_str(item: dict) -> str:
    """从收藏夹条目中提取 ID 字符串。

    优先 ``collects_id_str`` — 抖音 API 返回的 ``collects_id`` 是 JS 数字，
    超过 2⁵³ 后精度丢失，必须用字符串版本才能正确调用下游接口。
    """
    return str(
        item.get("collects_id_str")
        or item.get("collects_id")
        or item.get("id")
        or ((item.get("collects_info") or {}).get("collects_id_str"))
        or ((item.get("collects_info") or {}).get("collects_id"))
        or ""
    )


def _extract_collect_count(item: dict) -> str:
    """从收藏夹条目中提取作品数量，尝试多个可能的字段名。

    抖音 API 实际返回 ``total_number``（收藏夹列表接口）。
    """
    # 主字段（抖音实际返回的字段名）
    for key in ("total_number", "total", "video_count", "aweme_count",
                "count", "item_count", "collects_count", "media_count"):
        val = item.get(key)
        if val is not None and str(val).isdigit():
            return str(val)
    # 嵌套字段
    for wrapper in ("collects_info", "extra", "stats"):
        inner = item.get(wrapper)
        if isinstance(inner, dict):
            for key in ("total_number", "total", "video_count", "aweme_count",
                        "count", "item_count"):
                val = inner.get(key)
                if val is not None and str(val).isdigit():
                    return str(val)
    return "?"


async def _fetch_collect_map(api_client, collect_ids: list) -> dict:
    """为给定的收藏夹 ID 列表获取 {id: name} 映射。"""
    all_collects = await _fetch_all_collects(api_client)
    id_to_name: dict = {}
    for item in all_collects:
        cid = _extract_collects_id_str(item)
        if cid and cid in set(collect_ids):
            name = (
                item.get("collects_name")
                or item.get("name")
                or item.get("title")
                or cid
            )
            id_to_name[cid] = name
    # 补充 API 返回中未找到的 ID（可能用户手动指定了 ID）
    for cid in collect_ids:
        if cid not in id_to_name:
            id_to_name[cid] = cid
    return id_to_name


async def _run_serve_subcommand(args, config: ConfigLoader) -> None:
    """启动 REST API 服务模式（fastapi + uvicorn 为可选依赖）。"""
    try:
        from server.app import run_server
    except ImportError as exc:
        display.print_error(
            f"REST 服务模式需要安装可选依赖 fastapi + uvicorn："
            f"\n  pip install fastapi uvicorn\n原始错误：{exc}"
        )
        return

    display.print_info(f"启动 REST 服务：http://{args.serve_host}:{args.serve_port}")
    await run_server(config, host=args.serve_host, port=args.serve_port)


async def _run_sync_command(args, config: ConfigLoader) -> None:
    """执行同步命令"""
    if not config.validate():
        display.print_error("Invalid configuration: missing required fields")
        return

    cookies = config.get_cookies()
    cookie_manager = CookieManager()
    cookie_manager.set_cookies(cookies)

    if not cookie_manager.validate_cookies():
        display.print_warning("Cookies may be invalid or incomplete")

    database = None
    if config.get("database"):
        db_path = config.get("database_path", "dy_downloader.db") or "dy_downloader.db"
        database = Database(db_path=str(db_path))
        await database.initialize()
        display.print_success("Database initialized")

    # 创建API客户端
    async with DouyinAPIClient(
        cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        # 创建同步管理器
        sync_manager = SyncManager(api_client, database, config.config)

        if args.sync_once:
            # 执行一次同步
            display.print_info("Executing one-time sync...")
            result = await sync_manager.sync_collects()

            if result.get("status") == "completed":
                display.print_success("Sync completed successfully")
                display.print_info(f"Sync ID: {result.get('sync_id')}")
                display.print_info(f"Total videos: {result.get('total_videos')}")
                display.print_info(f"Processed videos: {result.get('processed_videos')}")
                display.print_info(f"Duration: {result.get('duration_seconds')} seconds")
            else:
                display.print_error(f"Sync failed: {result.get('error')}")

            if database:
                await database.close()
            return

        # 启动定时同步
        if not config.get("sync", {}).get("enabled", False):
            display.print_warning("Sync is disabled in config. Enable it first.")
            if database:
                await database.close()
            return

        # 更新cron表达式（如果通过参数指定）
        sync_config = config.get("sync", {}).copy()
        if args.sync_cron:
            sync_config["cron_expression"] = args.sync_cron
            config.update(sync=sync_config)
            config.save()  # 保存到配置文件

        display.print_info(f"Starting sync scheduler with cron: {sync_config.get('cron_expression')}")

        # 创建调度器
        scheduler = SyncScheduler(sync_manager, config.config)

        # 启动调度器
        scheduler.start()

        try:
            # 显示状态
            status = await scheduler.get_status()
            display.print_info(f"Scheduler started: {status}")

            # 显示同步历史
            history = await scheduler.get_sync_history(limit=5)
            if history:
                display.print_info("\nRecent sync history:")
                for sync in history:
                    display.print_info(f"  - {sync['sync_id']}: {sync['status']} at {sync['created_at']}")

            # 保持运行（按Ctrl+C停止）
            if args.sync_cron:
                display.print_info("Press Ctrl+C to stop the scheduler")
                while True:
                    await asyncio.sleep(1)
            else:
                # 如果没有指定cron，只运行一次
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            display.print_warning("\nSync scheduler interrupted by user")
        finally:
            await scheduler.stop()
            if database:
                await database.close()


async def _dispatch_notifications(config: ConfigLoader, total_result: Any, url_count: int) -> None:
    notifier = build_notifier(config)
    if not notifier.enabled:
        return

    if total_result is None:
        title = "抖音下载器：全部失败"
        body = f"共处理 {url_count} 个链接，无成功结果"
        level = "failure"
    else:
        fail_or_partial = total_result.failed > 0 or total_result.success == 0
        level = "failure" if fail_or_partial else "success"
        title = "抖音下载完成" if level == "success" else "抖音下载部分失败"
        body = (
            f"链接 {url_count} / 总作品 {total_result.total} / "
            f"成功 {total_result.success} / 失败 {total_result.failed} / "
            f"跳过 {total_result.skipped}"
        )

    try:
        summary = await notifier.send(title=title, body=body, level=level)
        if summary:
            succ = sum(1 for ok in summary.values() if ok)
            logger.info(
                "Notification dispatched to %d provider(s), %d ok",
                len(summary),
                succ,
            )
    except Exception as exc:  # 通知失败不应影响主流程
        logger.warning("Notification dispatch error: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="Douyin Downloader - 抖音批量下载工具")
    parser.add_argument("-u", "--url", action="append", help="Download URL(s)")
    parser.add_argument("-c", "--config", help="Config file path (default: config.yml)")
    parser.add_argument("-p", "--path", help="Save path")
    parser.add_argument("-t", "--thread", type=int, help="Thread count")
    parser.add_argument("--show-warnings", action="store_true", help="Show warning logs in console")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose console logs")
    parser.add_argument(
        "--hot-board",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="N",
        help="拉取抖音热搜榜并导出 JSONL，可选上限 N（默认全部）",
    )
    parser.add_argument(
        "--search",
        type=str,
        default=None,
        metavar="KEYWORD",
        help="按关键词搜索作品并导出 JSONL",
    )
    parser.add_argument(
        "--search-max",
        type=int,
        default=50,
        help="--search 场景下最多拉取条数（默认 50）",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="以 REST API 服务模式运行（需要安装 fastapi + uvicorn）",
    )
    parser.add_argument("--serve-host", type=str, default="127.0.0.1", help="REST 服务监听地址")
    parser.add_argument("--serve-port", type=int, default=8000, help="REST 服务监听端口")
    parser.add_argument("--sync", action="store_true", help="执行收藏夹同步")
    parser.add_argument("--sync-once", action="store_true", help="执行一次同步后退出")
    parser.add_argument("--sync-cron", type=str, help="设置同步的cron表达式")
    parser.add_argument(
        "--list-collections",
        action="store_true",
        help="列出当前账号所有收藏夹（ID 和名称）后退出",
    )
    parser.add_argument(
        "--collect-ids",
        type=str,
        default=None,
        metavar="ID1,ID2,...",
        help="指定要下载的收藏夹 ID 列表（逗号分隔，仅 collect 模式有效）",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="交互式选择要下载的收藏夹（列出所有收藏夹后提示选择）",
    )
    try:
        from __init__ import __version__
    except ImportError:
        __version__ = "2.0.0"
    parser.add_argument("--version", action="version", version=__version__)

    args = parser.parse_args()

    if args.verbose:
        set_console_log_level(logging.INFO)
    elif args.show_warnings:
        set_console_log_level(logging.WARNING)
    else:
        set_console_log_level(logging.ERROR)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        display.print_warning("\nDownload interrupted by user")
        sys.exit(0)
    except Exception as e:
        display.print_error(f"Fatal error: {e}")
        logger.exception("Fatal error occurred")
        sys.exit(1)


if __name__ == "__main__":
    main()
