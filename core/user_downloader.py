from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Set, Union

from core.downloader_base import BaseDownloader, DownloadResult
from core.user_mode_registry import UserModeRegistry
from utils.logger import setup_logger

logger = setup_logger("UserDownloader")


def _transcript_fail_label(transcript: Dict[str, Any]) -> str:
    """从转录结果中提取简短的失败原因标签。"""
    reason = transcript.get("reason", "")
    if reason:
        return reason
    error = str(transcript.get("error", ""))
    # 按常见错误特征归类
    if "ffmpeg" in error.lower() or "extract" in error.lower():
        return "audio_extract_failed"
    if "whisper" in error.lower() or "model" in error.lower():
        return "whisper_error"
    if "timeout" in error.lower():
        return "timeout"
    return "transcription_error"


def _normalise_collect_ids(raw: Union[str, List[str], None]) -> List[str]:
    """Accept comma-separated string or list, return a deduplicated list of
    non-empty collection IDs."""
    if raw is None:
        return []
    if isinstance(raw, str):
        ids = [cid.strip() for cid in raw.split(",") if cid.strip()]
    elif isinstance(raw, (list, tuple, set)):
        ids = [str(cid).strip() for cid in raw if str(cid).strip()]
    else:
        return []
    # Preserve insertion order while deduplicating.
    seen: set[str] = set()
    result: List[str] = []
    for cid in ids:
        if cid not in seen:
            seen.add(cid)
            result.append(cid)
    return result


class UserDownloader(BaseDownloader):
    SELF_COLLECT_MODES = {"collect", "collectmix"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode_registry = UserModeRegistry()
        self._mode_strategy_cache: Dict[str, Any] = {}

    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()

        sec_uid = parsed_url.get("sec_uid")
        if not sec_uid:
            # URL parser already validates this; treat as fatal instead of
            # a silent empty result so the UI surfaces a real error rather
            # than "已完成 0 项".
            raise RuntimeError("无法从链接中解析出用户 ID，请确认链接是否完整")

        modes_config = self.config.get("mode", ["post"])
        if isinstance(modes_config, str):
            modes = [modes_config]
        elif isinstance(modes_config, list):
            modes = [str(mode).strip() for mode in modes_config if str(mode).strip()]
        else:
            modes = ["post"]

        if not self._validate_mode_scope(sec_uid, modes):
            return result

        user_info = await self._resolve_user_info(sec_uid, modes)
        if not user_info:
            logger.error("Failed to get user info: %s", sec_uid)
            # Raising here instead of returning an empty result means the
            # job ends in `failed` state with a clear message. Returning
            # {total:0,success:0,failed:0} made JobManager mark it as
            # `success`, which rendered as "已完成 0 项" — a silent failure
            # that's indistinguishable from "nothing happened" in the UI.
            raise RuntimeError("获取用户信息失败，请检查 Cookie 是否有效或重新登录抖音")

        # Cache author metadata on the hosting job so retry doesn't have
        # to re-fetch user_info, and so JobRow can display the nickname.
        self._progress_report_author(
            nickname=user_info.get("nickname"),
            sec_uid=user_info.get("sec_uid") or sec_uid,
        )

        self._progress_update_step("下载模式", f"模式: {', '.join(modes)}")

        seen_aweme_ids: Set[str] = set()
        for mode in modes:
            strategy = self._get_mode_strategy(mode)
            if strategy is None:
                logger.warning("Unsupported user mode: %s", mode)
                continue

            self._progress_update_step("下载模式", f"开始处理 {mode} 作品")
            mode_result = await strategy.download_mode(
                sec_uid, user_info, seen_aweme_ids=seen_aweme_ids
            )
            result.merge(mode_result)

        return result

    def _validate_mode_scope(self, sec_uid: str, modes: List[str]) -> bool:
        normalized_modes = {str(mode or "").strip() for mode in modes}
        has_collect_mode = bool(normalized_modes & self.SELF_COLLECT_MODES)
        has_regular_mode = bool(normalized_modes - self.SELF_COLLECT_MODES)

        if has_collect_mode and sec_uid != "self":
            # Desktop "我的内容 / 下载本收藏夹" sends the real self sec_uid
            # together with a ``collects_id`` filter — by the time the
            # request reaches here the sidecar has already verified via
            # the cookie scope (``_resolve_viewer_sec_uid``) that the
            # caller is the logged-in user, so a real sec_uid + collect
            # mode + collects_id is the legit my-content path. Without
            # this branch ``download()`` would short-circuit and produce
            # an empty DownloadResult, which the JobManager renders as
            # the silent "已完成 0 项" failure.
            collects_id = (str(self.config.get("collects_id") or "")).strip()
            collect_ids = self.config.get("collect_ids")
            if not collects_id and not collect_ids:
                logger.error(
                    "Modes collect/collectmix only support "
                    "/user/self?showTab=favorite_collection or "
                    "my-content 下载本收藏夹 (collects_id required), "
                    "or --collect-ids to select specific collections"
                )
                return False
        if has_collect_mode and has_regular_mode:
            logger.error("Modes collect/collectmix cannot be combined with post/like/mix/music")
            return False
        return True

    def _filter_pinned_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self._download_pinned_enabled():
            return items
        return [item for item in items if not self._is_pinned_aweme(item)]

    def _download_pinned_enabled(self) -> bool:
        return self._as_bool(self.config.get("download_pinned", False))

    @staticmethod
    def _is_pinned_aweme(item: Dict[str, Any]) -> bool:
        value = item.get("is_top")
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    async def _resolve_user_info(self, sec_uid: str, modes: List[str]) -> Optional[Dict[str, Any]]:
        normalized_modes = {str(mode or "").strip() for mode in modes}
        if sec_uid == "self" and normalized_modes.issubset(self.SELF_COLLECT_MODES):
            self._progress_update_step("获取作者信息", "使用当前登录账号收藏夹上下文")
            return {
                "uid": "self",
                "sec_uid": "self",
                "nickname": "self",
            }

        # Desktop my-content "下载本收藏夹" path: real sec_uid + collect
        # mode + collects_id filter. The cookie scope upstream already
        # guarantees this is the viewer themselves, so we can skip the
        # network round-trip via ``api_client.get_user_info``.
        if (
            normalized_modes.issubset(self.SELF_COLLECT_MODES)
            and (str(self.config.get("collects_id") or "")).strip()
        ):
            self._progress_update_step("获取作者信息", "使用当前登录账号收藏夹上下文")
            return {
                "uid": sec_uid,
                "sec_uid": sec_uid,
                "nickname": "self",
            }

        self._progress_update_step("获取作者信息", f"sec_uid={sec_uid}")
        return await self.api_client.get_user_info(sec_uid)

    def _get_mode_strategy(self, mode: str):
        normalized_mode = (mode or "").strip()

        # The "collect" strategy supports an optional ``collects_id`` filter
        # that constrains paging to a single folder (desktop "我的收藏 / 下载
        # 本收藏夹"). When the filter is set we bypass the cache so the next
        # call with a different (or absent) filter doesn't reuse a stale
        # strategy bound to the previous folder. The no-filter path keeps
        # caching to preserve the existing CLI behaviour.
        if normalized_mode == "collect":
            return self._make_collect_strategy()

        if normalized_mode in self._mode_strategy_cache:
            return self._mode_strategy_cache[normalized_mode]

        strategy_cls = self.mode_registry.get(normalized_mode)
        if strategy_cls is None:
            return None

        strategy = strategy_cls(self)
        self._mode_strategy_cache[normalized_mode] = strategy
        return strategy

    def _make_collect_strategy(self):
        """Construct the collect strategy, threading ``collects_id`` or
        ``collect_ids`` from the per-job config when present. Caches only
        the no-filter path (matching the historic CLI behaviour) so a
        subsequent call with a different filter doesn't pick up a stale
        binding.
        """
        strategy_cls = self.mode_registry.get("collect")
        if strategy_cls is None:
            return None

        raw_filter = self.config.get("collects_id")
        collects_id = (str(raw_filter).strip() if raw_filter is not None else "") or None

        # Multi-collection mode: a list of IDs with optional name map.
        raw_ids = self.config.get("collect_ids")
        if raw_ids and not collects_id:
            collect_ids = _normalise_collect_ids(raw_ids)
            collect_map = self.config.get("collect_map") or {}
            if isinstance(collect_map, dict):
                collect_map = {str(k): str(v) for k, v in collect_map.items()}
            else:
                collect_map = {}
            # Multi-collection strategy is request-scoped — never cached.
            return strategy_cls(self, collect_ids=collect_ids, collect_map=collect_map)

        if collects_id is None:
            cached = self._mode_strategy_cache.get("collect")
            if cached is not None:
                return cached
            strategy = strategy_cls(self)
            self._mode_strategy_cache["collect"] = strategy
            return strategy

        # Single-folder filter path is request-scoped — never cached.
        return strategy_cls(self, collects_id=collects_id)

    async def _download_mode_items(
        self,
        mode: str,
        items: List[Dict[str, Any]],
        author_name: str,
        seen_aweme_ids: Optional[Set[str]] = None,
    ) -> DownloadResult:
        if seen_aweme_ids is None:
            seen_aweme_ids = set()
        deduped_items: List[Dict[str, Any]] = []
        local_seen: Set[str] = set()

        for item in items:
            aweme_id = str(item.get("aweme_id") or "").strip()
            if not aweme_id:
                continue
            if aweme_id in seen_aweme_ids or aweme_id in local_seen:
                continue
            local_seen.add(aweme_id)
            seen_aweme_ids.add(aweme_id)
            deduped_items.append(item)

        result = DownloadResult()
        result.total = len(deduped_items)

        # ── 转录流水线：独立消费者，下载完成后不阻塞槽位 ────────────
        transcript_cfg = self.config.get("transcript", {}) or {}
        transcription_enabled = bool(transcript_cfg.get("enabled", False))
        transcribe_queue: Optional[asyncio.Queue] = None
        consumer_task: Optional[asyncio.Task] = None
        transcribe_stats = {"done": 0, "total": 0}

        # 进度条 total = 下载 + 转录（各占一半），display_total 保持实际作品数
        if transcription_enabled:
            item_total = result.total * 2 if result.total > 0 else 1
            self._progress_set_item_total(item_total, "作品待下载", display_total=result.total)
        else:
            self._progress_set_item_total(result.total, "作品待下载")
        self._progress_update_step("下载作品", f"待处理 {result.total} 条")

        # Accumulate per-aweme DB records and flush in a single transaction
        # at the end — avoids one fsync per item across the whole batch.
        db_batch: Optional[List[Dict[str, Any]]] = [] if self.database else None

        if transcription_enabled:
            transcribe_queue = asyncio.Queue()

            async def _transcribe_consumer():
                while True:
                    item = await transcribe_queue.get()
                    if item is None:  # sentinel — 全部下载已入队
                        transcribe_queue.task_done()
                        break
                    video_path, aweme_id = item
                    try:
                        result = await self.transcript_manager.process_video(
                            video_path, aweme_id=aweme_id
                        )
                    except Exception as exc:
                        result = {
                            "status": "failed",
                            "reason": "transcription_error",
                            "error": str(exc),
                        }
                    self._transcript_results[str(aweme_id)] = result
                    transcribe_stats["done"] += 1
                    # 推送转录进度（不影响下载 S/F/K 统计）
                    self._progress_advance_transcribe(aweme_id)
                    if transcribe_stats["total"] > 0:
                        self._progress_update_step(
                            "转录作品",
                            f"转录中 {transcribe_stats['done']}/{transcribe_stats['total']}",
                        )
                    t_status = result.get("status")
                    if t_status == "skipped":
                        logger.info(
                            "Transcript skipped for aweme %s: %s",
                            aweme_id,
                            result.get("reason", "unknown"),
                        )
                    elif t_status == "failed":
                        logger.warning(
                            "Transcript failed for aweme %s: %s",
                            aweme_id,
                            result.get("error", "unknown"),
                        )
                    transcribe_queue.task_done()

            consumer_task = asyncio.ensure_future(_transcribe_consumer())

        async def _process_aweme(item: Dict[str, Any]):
            aweme_id = str(item.get("aweme_id") or "")
            desc = (item.get("desc") or "").strip() or aweme_id
            if not await self._should_download(aweme_id):
                self._progress_advance_item("skipped", aweme_id)
                # 视频已下载但转录未完成时，仍然推入转录队列
                if transcription_enabled and self._detect_media_type(item) == "video":
                    local_video = self._find_local_media_by_aweme_id(aweme_id)
                    if local_video is not None and transcribe_queue is not None:
                        # 先检查转录是否已存在，避免重复入队
                        txt_path, _ = self.transcript_manager.build_output_paths(local_video)
                        if not txt_path.is_file():
                            await transcribe_queue.put((local_video, aweme_id))
                            transcribe_stats["total"] += 1
                            logger.info(
                                "已下载视频 %s 推入转录队列: %s", aweme_id, local_video.name,
                            )
                return {"status": "skipped", "aweme_id": aweme_id, "desc": desc}

            is_video = self._detect_media_type(item) == "video"
            dl_t0 = time.monotonic()
            success = await self._download_aweme_assets(
                item, author_name, mode=mode, db_batch=db_batch,
                transcribe=False,
                transcribe_queue=transcribe_queue,
            )
            dl_elapsed = round(time.monotonic() - dl_t0, 1)
            status = "success" if success else "failed"
            self._progress_advance_item(status, aweme_id)
            if success and transcription_enabled and is_video:
                transcribe_stats["total"] += 1
            return {
                "status": status,
                "aweme_id": aweme_id,
                "desc": desc,
                "download_duration": dl_elapsed,
            }

        download_results = await self.queue_manager.download_batch(_process_aweme, deduped_items)

        # ── 等待转录全部完成 ────────────────────────────────────────
        if consumer_task is not None:
            transcribe_count = transcribe_stats["total"]
            self._progress_update_step(
                "转录作品",
                f"下载完成，开始转录 {transcribe_count} 个视频...",
            )
            await transcribe_queue.put(None)   # 发送 sentinel
            await consumer_task                # 等待消费者退出
            self._progress_update_step("转录作品", "转录完成")

        if db_batch:
            await self.database.add_aweme_batch(db_batch)

        for entry in download_results:
            entry = entry if isinstance(entry, dict) else {}
            status = entry.get("status")
            aweme_id = str(entry.get("aweme_id") or "")
            desc = str(entry.get("desc") or aweme_id)
            dl_dur = entry.get("download_duration")

            if status == "success":
                result.success += 1
            elif status == "failed":
                result.failed += 1
                result.add_issue(aweme_id, desc, download="failed", download_duration=dl_dur)
            elif status == "skipped":
                result.skipped += 1
                result.add_issue(aweme_id, desc, download="skipped")
            else:
                result.failed += 1
                self._progress_advance_item("failed", "unknown")
                result.add_issue(aweme_id, desc, download="failed")

            # 累积转录统计 + 异常明细
            transcript = self._transcript_results.pop(aweme_id, None)
            if isinstance(transcript, dict):
                t_status = transcript.get("status")
                t_dur = transcript.get("duration")
                if t_status == "success":
                    result.add_transcript_success()
                elif t_status == "skipped":
                    reason = transcript.get("reason", "unknown")
                    result.add_transcript_skip(reason)
                    result.add_issue(
                        aweme_id, desc,
                        download=None if status == "success" else status,
                        transcript="skipped", transcript_reason=reason,
                        transcript_duration=t_dur,
                        transcript_error=str(transcript.get("error", "")),
                    )
                elif t_status == "failed":
                    reason = _transcript_fail_label(transcript)
                    error_msg = str(transcript.get("error", "") or "")
                    result.add_transcript_fail(reason)
                    result.add_issue(
                        aweme_id, desc,
                        download=None if status == "success" else status,
                        transcript="failed", transcript_reason=reason,
                        transcript_duration=t_dur,
                        transcript_error=error_msg,
                    )

        return result

    # 向后兼容：旧测试仍直接调用 post 下载入口。
    async def _download_user_post(self, sec_uid: str, user_info: Dict[str, Any]) -> DownloadResult:
        strategy = self._get_mode_strategy("post")
        if strategy is None:
            return DownloadResult()
        return await strategy.download_mode(sec_uid, user_info, seen_aweme_ids=set())

    async def _recover_user_post_with_browser(
        self,
        sec_uid: str,
        user_info: Dict[str, Any],
        aweme_list: List[Dict[str, Any]],
    ) -> None:
        browser_cfg = self.config.get("browser_fallback", {}) or {}
        if not browser_cfg.get("enabled", True):
            return

        number_limit = self.config.get("number", {}).get("post", 0)
        # 在分页受限场景下，user_info.aweme_count 常常不可靠（经常只返回 20）
        # 因此仅在用户显式设置 number_limit 时才限制浏览器采集目标数量。
        expected_count = int(number_limit or 0)
        if expected_count and len(aweme_list) >= expected_count:
            return

        try:
            browser_aweme_ids = await self.api_client.collect_user_post_ids_via_browser(
                sec_uid,
                expected_count=expected_count,
                headless=bool(browser_cfg.get("headless", False)),
                max_scrolls=int(browser_cfg.get("max_scrolls", 240) or 240),
                idle_rounds=int(browser_cfg.get("idle_rounds", 8) or 8),
                wait_timeout_seconds=int(browser_cfg.get("wait_timeout_seconds", 600) or 600),
            )
        except Exception as exc:
            logger.error("Browser fallback failed: %s", exc)
            return

        browser_aweme_items: Dict[str, Dict[str, Any]] = {}
        browser_post_stats: Dict[str, int] = {}
        if hasattr(self.api_client, "pop_browser_post_aweme_items"):
            try:
                browser_aweme_items = self.api_client.pop_browser_post_aweme_items() or {}
            except Exception as exc:
                logger.debug("Fetch browser post items skipped: %s", exc)
        if hasattr(self.api_client, "pop_browser_post_stats"):
            try:
                browser_post_stats = self.api_client.pop_browser_post_stats() or {}
            except Exception as exc:
                logger.debug("Fetch browser post stats skipped: %s", exc)

        if not browser_aweme_ids:
            logger.warning("Browser fallback returned no aweme_id")
            return

        existing_ids = {str(item.get("aweme_id")) for item in aweme_list if item.get("aweme_id")}
        missing_ids = [aweme_id for aweme_id in browser_aweme_ids if aweme_id not in existing_ids]
        if not missing_ids:
            return

        logger.warning(
            "Recovering aweme details from browser list, missing count=%s",
            len(missing_ids),
        )
        detail_failed = 0
        detail_success = 0
        reused_from_browser_items = 0
        total_missing = len(missing_ids)
        for index, aweme_id in enumerate(missing_ids, start=1):
            if number_limit > 0 and len(aweme_list) >= number_limit:
                break

            if index == 1 or index == total_missing or index % 5 == 0:
                self._progress_update_step("浏览器回补", f"补全详情 {index}/{total_missing}")

            detail = browser_aweme_items.get(str(aweme_id))
            if not detail:
                await self.rate_limiter.acquire()
                detail = await self.api_client.get_video_detail(aweme_id, suppress_error=True)
                if detail:
                    detail_success += 1
            else:
                reused_from_browser_items += 1
            if not detail:
                detail_failed += 1
                continue
            author = detail.get("author", {}) if isinstance(detail, dict) else {}
            detail_sec_uid = author.get("sec_uid") if isinstance(author, dict) else None
            if detail_sec_uid and str(detail_sec_uid) != str(sec_uid):
                logger.warning(
                    "Skip aweme_id=%s due to mismatched sec_uid (%s)",
                    aweme_id,
                    detail_sec_uid,
                )
                continue
            aweme_list.append(detail)

        self._progress_update_step(
            "浏览器回补",
            f"回补完成，复用 {reused_from_browser_items}，补拉成功 {detail_success}，失败 {detail_failed}",
        )
        logger.warning(
            "Browser fallback summary: merged_ids=%s selected_ids=%s post_items=%s post_pages=%s reused=%s detail_success=%s detail_failed=%s",
            browser_post_stats.get("merged_ids", 0),
            browser_post_stats.get("selected_ids", len(browser_aweme_ids)),
            browser_post_stats.get("post_items", len(browser_aweme_items)),
            browser_post_stats.get("post_pages", 0),
            reused_from_browser_items,
            detail_success,
            detail_failed,
        )

        if detail_failed > 0:
            logger.warning(
                "Browser fallback detail fetch failed: %s/%s",
                detail_failed,
                total_missing,
            )
