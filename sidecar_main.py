#!/usr/bin/env python3
"""Sidecar JSON-RPC entry point — Rust 通过 stdin/stdout 驱动核心功能。

协议（每行一个 JSON）：
  请求:  {"id": "...", "method": "...", "params": {...}}
  成功:  {"id": "...", "ok": true,  "result": ...}
  失败:  {"id": "...", "ok": false, "error": "..."}
  进度:  {"id": "...", "type": "progress", "step": "...", "detail": "..."}

支持的方法:
  - ping              连通性检测
  - list_collections  列出当前账号所有收藏夹
  - download_collection  下载指定收藏夹视频（返回文件列表）
  - transcribe        对单个视频进行转录
  - shutdown          退出进程
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)


def _log(msg: str) -> None:
    """写入 stderr，Rust 端只读 stderr 作为日志通道。"""
    print(f"[sidecar] {msg}", file=sys.stderr, flush=True)


# 提升控制台日志级别到 INFO，确保下载进度信息可见
from utils.logger import set_console_log_level  # noqa: E402
import logging  # noqa: E402
set_console_log_level(logging.INFO)
_log("sidecar 启动，日志级别已设为 INFO")

# 预加载 CUDA / Whisper —— 必须在事件循环启动前完成。
# 在长期运行的 asyncio 进程中首次 import ctranslate2 会导致 CUDA 驱动
# 初始化卡死，提前在这里同步完成避免后续转录步骤挂起。
_preload_cuda = os.environ.get("DOUYIN_SKIP_CUDA_PRELOAD", "").strip().lower() not in ("1", "true", "yes")
if _preload_cuda:
    _log("预加载 CUDA...")
    try:
        import ctranslate2  # noqa: F401
        _log(f"ctranslate2 {ctranslate2.__version__}, CUDA 设备数={ctranslate2.get_cuda_device_count()}")
    except Exception as exc:
        _log(f"CUDA 预加载失败（将回退 CPU 或跳过转录）: {exc}")
        # 不阻断启动，转录时再报错
else:
    _log("跳过 CUDA 预加载（DOUYIN_SKIP_CUDA_PRELOAD=1）")


# ---------------------------------------------------------------------------
# 懒加载模块 — 仅在首次调用时 import，加速 ping 响应
# ---------------------------------------------------------------------------
_imports: Dict[str, Any] = {}


def _lazy(name: str):
    if name not in _imports:
        if name == "CookieManager":
            from auth import CookieManager as cls

            _imports[name] = cls
        elif name == "ConfigLoader":
            from config import ConfigLoader as cls

            _imports[name] = cls
        elif name == "DouyinAPIClient":
            from core.api_client import DouyinAPIClient as cls

            _imports[name] = cls
        elif name == "DownloaderFactory":
            from core.downloader_factory import DownloaderFactory as cls

            _imports[name] = cls
        elif name == "URLParser":
            from core.url_parser import URLParser as cls

            _imports[name] = cls
        elif name == "FileManager":
            from storage.file_manager import FileManager as cls

            _imports[name] = cls
        elif name == "Database":
            from storage.database import Database as cls

            _imports[name] = cls
        elif name == "RateLimiter":
            from control.rate_limiter import RateLimiter as cls

            _imports[name] = cls
        elif name == "RetryHandler":
            from control.retry_handler import RetryHandler as cls

            _imports[name] = cls
        elif name == "QueueManager":
            from control.queue_manager import QueueManager as cls

            _imports[name] = cls
        elif name == "TranscriptManager":
            from core.transcript_manager import TranscriptManager as cls

            _imports[name] = cls
        else:
            raise RuntimeError(f"Unknown lazy import: {name}")
    return _imports[name]


# ---------------------------------------------------------------------------
# 方法实现
# ---------------------------------------------------------------------------

# 当前正在处理的请求 ID（用于进度通知带上关联 ID）
_current_request_id: str = ""


def _notify(step: str, detail: str) -> None:
    """发送进度通知（作为 JSON 行写入 stdout，与响应行格式一致但多 type 字段）。

    Rust 端通过 ``"type": "progress"`` 区分进度通知和最终响应。
    """
    global _current_request_id
    msg = {
        "id": _current_request_id,
        "type": "progress",
        "step": step,
        "detail": detail,
    }
    _write_resp(msg)


class _SidecarProgressReporter:
    """桥接 downloader 的 ProgressReporter 协议 → sidecar _notify()。"""

    def update_step(self, step: str, detail: str = "") -> None:
        _notify(step, detail)

    def set_item_total(self, total: int, detail: str = "") -> None:
        _notify("progress", f"共 {total} 个作品 {detail}".strip())

    def advance_item(self, status: str, detail: str = "") -> None:
        label = {"success": "✓", "failed": "✗", "skipped": "⊙"}.get(status, status)
        _notify("download_item", f"{label} {detail}")


async def _ping(_params: Dict[str, Any]) -> Dict[str, Any]:
    return {"pong": True, "cwd": str(PROJECT_ROOT)}


async def _list_collections(params: Dict[str, Any]) -> Dict[str, Any]:
    """列出当前账号所有收藏夹。

    params:
      config_path: str  配置文件路径（默认 config.yml）
    返回:
      collections: [{"id": str, "name": str, "count": int}, ...]
    """
    config_path = params.get("config_path", "config.yml")
    Ct = _lazy("ConfigLoader")
    config = Ct(config_path)

    cookies = config.get_cookies()

    Cm = _lazy("CookieManager")
    cm = Cm()
    cm.set_cookies(cookies)

    Ac = _lazy("DouyinAPIClient")
    async with Ac(cm.get_cookies(), proxy=config.get("proxy")) as api:
        # 分页拉取
        all_items = []
        cursor = 0
        has_more = True
        while has_more:
            page = await api.get_user_collects("self", max_cursor=cursor, count=20)
            items = page.get("items") or page.get("collects_list") or []
            if not items:
                break
            all_items.extend(items)
            has_more = bool(page.get("has_more", False))
            nxt = int(page.get("max_cursor", 0) or 0)
            if has_more and nxt == cursor:
                break
            cursor = nxt

    collections = []
    for item in all_items:
        cid = str(
            item.get("collects_id_str")
            or item.get("collects_id")
            or item.get("id")
            or ""
        )
        cname = (
            item.get("collects_name")
            or item.get("name")
            or item.get("title")
            or "(未命名)"
        )
        count = str(
            item.get("total_number") or item.get("total") or ""
        )
        if cid:
            collections.append({"id": cid, "name": cname, "count": count})

    return {"collections": collections, "total": len(collections)}


async def _download_collection(params: Dict[str, Any]) -> Dict[str, Any]:
    """下载指定收藏夹的全部视频。

    params:
      config_path:  str  配置文件路径
      collects_id:  str  收藏夹 ID
      output_dir:   str  输出目录（可选，覆盖配置）
      max_count:    int  最多下载数（0 = 不限）
    """
    config_path = params.get("config_path", "config.yml")
    collects_id = params.get("collects_id", "")
    max_count = int(params.get("max_count", 0) or 0)

    if not collects_id:
        return {"error": "缺少 collects_id 参数"}

    Ct = _lazy("ConfigLoader")
    config = Ct(config_path)

    if "output_dir" in params:
        config.update(path=params["output_dir"])
    if max_count > 0:
        config.update(number={"collect": max_count})
    # 加速：禁用封面/头像/音乐下载，减少重试次数，避免路径双重嵌套
    config.update(
        mode=["collect"],
        cover=False,
        avatar=False,
        music=False,
        retry_times=2,
        group_by_mode=False,
        link=[f"https://www.douyin.com/collection/{collects_id}"],
    )

    cookies = config.get_cookies()
    Cm = _lazy("CookieManager")
    cm = Cm()
    cm.set_cookies(cookies)

    # 数据库
    Db = _lazy("Database")
    db = None
    if config.get("database"):
        db_path = config.get("database_path", "dy_downloader.db") or "dy_downloader.db"
        db = Db(db_path=str(db_path))
        await db.initialize()

    Fm = _lazy("FileManager")
    Rl = _lazy("RateLimiter")
    Rh = _lazy("RetryHandler")
    Qm = _lazy("QueueManager")
    Df = _lazy("DownloaderFactory")
    Up = _lazy("URLParser")
    Ac = _lazy("DouyinAPIClient")

    file_manager = Fm(config.get("path"))
    rate_limiter = Rl(max_per_second=float(config.get("rate_limit", 2) or 2))
    retry_handler = Rh(max_retries=2)
    queue_manager = Qm(max_workers=int(config.get("thread", 2) or 2))

    # 确保延迟导入的模块 logger 也被提升到 INFO 级别
    set_console_log_level(logging.INFO)

    downloaded_files = []

    async with Ac(cm.get_cookies(), proxy=config.get("proxy")) as api:
        url = f"https://www.douyin.com/collection/{collects_id}"
        parsed = Up.parse(url)
        if not parsed:
            return {"error": f"URL 解析失败: {url}"}

        progress_reporter = _SidecarProgressReporter()
        downloader = Df.create(
            parsed["type"],
            config,
            api,
            file_manager,
            cm,
            db,
            rate_limiter,
            retry_handler,
            queue_manager,
            progress_reporter=progress_reporter,
        )
        if not downloader:
            return {"error": f"未找到下载器: {parsed['type']}"}

        _notify("download_start", f"开始下载收藏夹 {collects_id}")
        result = await downloader.download(parsed)
        _notify("download_done", f"下载完成: 成功{result.success} 失败{result.failed}")

        # 收集下载的文件
        base = Path(config.get("path", "./Downloaded/"))
        for mp4 in sorted(base.rglob("*.mp4")):
            downloaded_files.append(str(mp4))

        outcome = {
            "status": "success",
            "total": result.total if result else 0,
            "success": result.success if result else 0,
            "failed": result.failed if result else 0,
            "skipped": result.skipped if result else 0,
            "files": downloaded_files[-20:],
            "file_count": len(downloaded_files),
        }

    if db:
        await db.close()

    return outcome


async def _transcribe(params: Dict[str, Any]) -> Dict[str, Any]:
    """对单个视频文件进行 Whisper 转录。

    params:
      config_path:     str  配置文件路径
      video_path:      str  视频文件绝对路径
      aweme_id:        str  作品 ID（用于去重记录）
      backend:         str  "whisper" 或 "openai"（可选，覆盖配置）
      whisper_model:   str  模型大小（可选）
    """
    video_path = params.get("video_path", "")
    aweme_id = params.get("aweme_id", "")

    if not video_path:
        return {"error": "缺少 video_path 参数"}
    if not aweme_id:
        aweme_id = Path(video_path).stem

    config_path = params.get("config_path", "config.yml")
    Ct = _lazy("ConfigLoader")
    config = Ct(config_path)

    # 允许调用方覆盖后端和模型
    if "backend" in params:
        config.update(transcript={**config.get("transcript", {}), "backend": params["backend"]})
    if "whisper_model" in params:
        config.update(transcript={**config.get("transcript", {}), "whisper_model": params["whisper_model"]})

    if not config.get("transcript", {}).get("enabled", False):
        return {"status": "skipped", "reason": "disabled"}

    Fm = _lazy("FileManager")
    file_manager = Fm(config.get("path", "./Downloaded/"))

    Tm = _lazy("TranscriptManager")
    transcript_manager = Tm(config, file_manager, database=None)

    _log(f"开始转录: {video_path}")
    result = await transcript_manager.process_video(Path(video_path), aweme_id)
    _log(f"转录完成: {result.get('status')}")
    return result


async def _shutdown(_params: Dict[str, Any]) -> Dict[str, Any]:
    return {"shutdown": True}


# ---------------------------------------------------------------------------
# 方法路由表
# ---------------------------------------------------------------------------
ROUTES = {
    "ping": _ping,
    "list_collections": _list_collections,
    "download_collection": _download_collection,
    "transcribe": _transcribe,
    "shutdown": _shutdown,
}


# ---------------------------------------------------------------------------
# stdin/stdout JSON-RPC 主循环
#
# 使用独立线程同步读取 stdin（Windows 兼容），通过 asyncio.Queue 传给
# 主事件循环处理。这样就避开了 asyncio connect_read_pipe 在 Windows 上
# 对 subprocess pipe EOF 的检测问题。
# ---------------------------------------------------------------------------
import threading  # noqa: E402
import queue  # noqa: E402


def _write_resp(obj: Dict[str, Any]) -> None:
    """写入一行 JSON 到 stdout（绕过 Windows 文本模式编码问题）。

    使用 ``sys.stdout.buffer.write`` + 显式 UTF-8 编码，确保 Rust 端
    读取到的字节流始终是合法 UTF-8。
    """
    data = json.dumps(obj, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(data.encode("utf-8"))
    sys.stdout.buffer.flush()


async def _process_forever(cmd_queue: asyncio.Queue) -> None:
    """主事件循环：从队列取命令，执行，写回响应。"""
    global _current_request_id
    while True:
        item = await cmd_queue.get()
        if item is None:  # 哨兵：stdin 关闭
            _log("stdin 已关闭，退出")
            break

        req_id, method, params = item
        _current_request_id = req_id

        handler = ROUTES.get(method)
        if handler is None:
            _write_resp({"id": req_id, "ok": False, "error": f"未知方法: {method}"})
            continue

        try:
            result = await handler(params)
            _write_resp({"id": req_id, "ok": True, "result": result})
        except Exception as exc:
            _log(f"方法 {method} 执行失败: {exc}")
            import traceback
            traceback.print_exc(file=sys.stderr)
            _write_resp({"id": req_id, "ok": False, "error": str(exc)})

        if method == "shutdown":
            _log("收到 shutdown 命令，退出")
            break


def _reader_thread(loop: asyncio.AbstractEventLoop, cmd_queue: asyncio.Queue) -> None:
    """运行在独立线程：同步阻塞读 stdin（UTF-8 字节），行放入 asyncio 队列。

    使用 ``sys.stdin.buffer`` 读原始字节再 decode("utf-8")，避免 Windows 上
    ``sys.stdin`` 默认按 GBK 解码导致中文路径乱码。
    """
    _log("sidecar 启动完成，等待命令...")
    try:
        for line_bytes in sys.stdin.buffer:
            line_str = line_bytes.decode("utf-8").strip()
            if not line_str:
                continue
            try:
                req = json.loads(line_str)
            except json.JSONDecodeError as exc:
                resp = {"id": None, "ok": False, "error": f"JSON parse error: {exc}"}
                _write_resp(resp)
                continue

            req_id = req.get("id", "")
            method = req.get("method", "")
            params = req.get("params", {})
            if not isinstance(params, dict):
                params = {}

            # 把命令放入 asyncio 队列（线程安全）
            loop.call_soon_threadsafe(cmd_queue.put_nowait, (req_id, method, params))

    except Exception as exc:
        _log(f"stdin 读取线程异常: {exc}")
    finally:
        # 通知主循环退出
        loop.call_soon_threadsafe(cmd_queue.put_nowait, None)


async def main_loop() -> None:
    cmd_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # 启动 stdin 读取线程
    t = threading.Thread(
        target=_reader_thread,
        args=(loop, cmd_queue),
        name="stdin-reader",
        daemon=True,
    )
    t.start()

    await _process_forever(cmd_queue)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(main_loop())
