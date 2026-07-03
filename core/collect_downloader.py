from __future__ import annotations

from typing import Any, Dict, Optional

from core.downloader_base import BaseDownloader, DownloadResult
from utils.logger import setup_logger

logger = setup_logger("CollectDownloader")


class CollectDownloader(BaseDownloader):
    """收藏夹下载器 - 用于服务器模式的收藏夹下载"""

    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()

        collects_id = parsed_url.get("collects_id")
        if not collects_id:
            logger.error("No collects_id found in parsed URL")
            return result

        # 获取收藏夹视频列表
        self._progress_update_step("获取列表", f"正在拉取收藏夹 {collects_id} 的作品列表...")
        aweme_list = []
        cursor = 0
        has_more = True
        page = 0

        while has_more:
            page += 1
            await self.rate_limiter.acquire()
            page_data = await self.api_client.get_collect_aweme(
                str(collects_id), max_cursor=cursor, count=20
            )

            page_items = page_data.get("aweme_list", [])
            if not page_items:
                break

            aweme_list.extend(page_items)
            self._progress_update_step("获取列表", f"已拉取 {len(aweme_list)} 个作品 (第{page}页)...")

            has_more = bool(page_data.get("has_more", False))
            next_cursor = int(page_data.get("max_cursor", 0) or 0)
            if has_more and next_cursor == cursor:
                logger.warning("Collect folder %s cursor did not advance", collects_id)
                break
            cursor = next_cursor

        # 应用数量限制
        number_config = self.config.get("number", {})
        limit = number_config.get("collect", 0)
        if limit > 0 and len(aweme_list) > limit:
            aweme_list = aweme_list[:limit]
            logger.info("Limited to %d items", limit)

        result.total = len(aweme_list)
        logger.info("Found %d videos in collection %s", result.total, collects_id)
        self._progress_set_item_total(result.total, f"收藏夹 {collects_id}")
        self._progress_update_step("下载作品", f"待处理 {result.total} 条")

        # 下载每个视频
        async def _process_aweme(item: Dict[str, Any]):
            aweme_id = item.get("aweme_id")
            if not aweme_id:
                return {"status": "failed", "aweme_id": None}

            author_name = "collect"
            success = await self._download_aweme_assets(item, author_name, mode="collect")
            status = "success" if success else "failed"
            self._progress_advance_item(status, str(aweme_id))
            return {"status": status, "aweme_id": aweme_id}

        download_results = await self.queue_manager.download_batch(_process_aweme, aweme_list)
        for entry in download_results:
            if entry.get("status") == "success":
                result.success += 1
            elif entry.get("status") == "failed":
                result.failed += 1
            elif entry.get("status") == "skipped":
                result.skipped += 1

        return result