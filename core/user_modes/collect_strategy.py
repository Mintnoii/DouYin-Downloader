from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.downloader_base import DownloadResult
from core.user_modes.base_strategy import BaseUserModeStrategy
from utils.logger import setup_logger
from utils.validators import sanitize_filename

logger = setup_logger("CollectUserModeStrategy")


class CollectUserModeStrategy(BaseUserModeStrategy):
    mode_name = "collect"
    api_method_name = "get_user_collects"

    def __init__(
        self,
        downloader,
        *,
        collects_id: Optional[str] = None,
        collect_ids: Optional[List[str]] = None,
        collect_map: Optional[Dict[str, str]] = None,
    ):
        """Strategy for downloading favourited / collected content.

        Parameters
        ----------
        collects_id:
            Constrains download to a single folder (desktop "下载本收藏夹").
            Mutually exclusive with ``collect_ids``.
        collect_ids:
            List of collection IDs to download. Each collection's videos
            land in a separate directory named ``{id}_{name}`` under the
            base download path. When ``None`` (and ``collects_id`` is also
            ``None``), the legacy behaviour applies: enumerate every folder
            and merge all videos into one output directory.
        collect_map:
            ``{collect_id: collect_name}`` mapping, pre-fetched by the
            caller (e.g. via ``api_client.get_user_collects``). Only
            meaningful when ``collect_ids`` is provided.
        """
        super().__init__(downloader)
        self._collects_id_filter = (collects_id or "").strip() or None
        self._collect_ids: Optional[List[str]] = (
            [cid for cid in collect_ids if cid and str(cid).strip()]
            if collect_ids
            else None
        )
        self._collect_map: Dict[str, str] = collect_map or {}

    async def collect_items(self, sec_uid: str, user_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._collects_id_filter:
            return await self._collect_single_folder(self._collects_id_filter)
        if self._collect_ids:
            # Multi-collection mode — handled in :meth:`download_mode`.
            return []
        return await self._collect_all_folders(sec_uid)

    async def download_mode(
        self,
        sec_uid: str,
        user_info: Dict[str, Any],
        seen_aweme_ids: Optional[set[str]] = None,
    ) -> DownloadResult:
        if not self._collect_ids:
            # Single-folder filter or legacy enumerate-all.
            return await super().download_mode(sec_uid, user_info, seen_aweme_ids)

        # ── Multi-collection mode ────────────────────────────────────
        # Each collection is downloaded into its own per-collection
        # directory so the user can tell which video came from which
        # folder.
        if seen_aweme_ids is None:
            seen_aweme_ids = set()

        total_result = DownloadResult()
        fetch_collect_aweme = getattr(self.downloader.api_client, "get_collect_aweme", None)
        if not callable(fetch_collect_aweme):
            logger.warning("API client missing get_collect_aweme")
            return total_result

        for cid in self._collect_ids:
            cname = self._collect_map.get(cid, cid)
            safe_cname = sanitize_filename(cname)
            # Path:  Downloaded/collect/{id}_{name}/{folder_template}/
            # Achieved by setting author_name="collect" and the
            # collection-specific label as the "mode" sub-directory.
            mode_label = f"{cid}_{safe_cname}" if safe_cname else cid

            logger.info("Downloading collection [%s] (%s) → collect/%s", cid, cname, mode_label)

            items = await self._collect_single_folder(cid)
            items = self.apply_filters(items)

            self.downloader._progress_update_step(
                "下载收藏夹", f"{cname}（{cid}）：{len(items)} 个作品"
            )

            mode_result = await self.downloader._download_mode_items(
                mode=mode_label,
                items=items,
                author_name="collect",
                seen_aweme_ids=seen_aweme_ids,
            )
            total_result.merge(mode_result)

        return total_result

    async def _collect_single_folder(self, collects_id: str) -> List[Dict[str, Any]]:
        """Paginate aweme entries for a single collection folder.

        Mirrors the inner loop of :meth:`_collect_all_folders` but
        intentionally avoids :meth:`api_client.get_user_collects` so we
        never even read the names of other folders on the account
        (Property 4 / R6.4 — single-folder filter does not leak entries
        from sibling folders).
        """
        fetch_collect_aweme = getattr(self.downloader.api_client, "get_collect_aweme", None)
        if not callable(fetch_collect_aweme):
            logger.warning("API client missing get_collect_aweme")
            return []

        expanded: List[Dict[str, Any]] = []
        seen_aweme: set[str] = set()

        cursor = 0
        has_more = True
        while has_more:
            await self.downloader.rate_limiter.acquire()
            page_data = await fetch_collect_aweme(str(collects_id), max_cursor=cursor, count=20)
            page = self._normalize_page_data(page_data)
            page_items = page.get("items", [])
            if not page_items:
                break

            for item in page_items:
                aweme = self._extract_aweme_from_item(item)
                if not aweme:
                    continue
                aweme_id = str(aweme.get("aweme_id") or "")
                if not aweme_id or aweme_id in seen_aweme:
                    continue
                seen_aweme.add(aweme_id)
                expanded.append(aweme)

            has_more = bool(page.get("has_more", False))
            next_cursor = int(page.get("max_cursor", 0) or 0)
            if has_more and next_cursor == cursor:
                logger.warning("Collect folder %s cursor did not advance", collects_id)
                break
            cursor = next_cursor

        return expanded

    async def _collect_all_folders(self, sec_uid: str) -> List[Dict[str, Any]]:
        """Original behaviour: enumerate every folder on the account and
        paginate each one. Kept as a separate method so the filter branch
        in :meth:`collect_items` doesn't accidentally invoke
        :meth:`api_client.get_user_collects`.
        """
        fetch_collect_aweme = getattr(self.downloader.api_client, "get_collect_aweme", None)
        fetch_collects = getattr(self.downloader.api_client, self.api_method_name, None)
        if not callable(fetch_collects):
            logger.warning("API client missing %s", self.api_method_name)
            return []
        if not callable(fetch_collect_aweme):
            logger.warning("API client missing get_collect_aweme")
            return []

        raw_collects = await self._collect_paged_entries(fetch_collects, sec_uid)
        expanded: List[Dict[str, Any]] = []
        seen_aweme: set[str] = set()

        for collect_item in raw_collects:
            collects_id = self._extract_collects_id(collect_item)
            if not collects_id:
                continue

            cursor = 0
            has_more = True
            while has_more:
                await self.downloader.rate_limiter.acquire()
                page_data = await fetch_collect_aweme(str(collects_id), max_cursor=cursor, count=20)
                page = self._normalize_page_data(page_data)
                page_items = page.get("items", [])
                if not page_items:
                    break

                for item in page_items:
                    aweme = self._extract_aweme_from_item(item)
                    if not aweme:
                        continue
                    aweme_id = str(aweme.get("aweme_id") or "")
                    if not aweme_id or aweme_id in seen_aweme:
                        continue
                    seen_aweme.add(aweme_id)
                    expanded.append(aweme)

                has_more = bool(page.get("has_more", False))
                next_cursor = int(page.get("max_cursor", 0) or 0)
                if has_more and next_cursor == cursor:
                    logger.warning("Collect folder %s cursor did not advance", collects_id)
                    break
                cursor = next_cursor

        return expanded

    @staticmethod
    def _extract_collects_id(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        # 优先 collects_id_str — 抖音 API 返回的 collects_id 是 JS 数字，
        # 精度受限（末尾被截断为 0000），调用下游接口必须用完整字符串。
        return str(
            item.get("collects_id_str")
            or item.get("collects_id")
            or item.get("id")
            or ((item.get("collects_info") or {}).get("collects_id_str"))
            or ((item.get("collects_info") or {}).get("collects_id"))
            or ""
        )
