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
        aweme_list = []
        cursor = 0
        has_more = True

        while has_more:
            await self.rate_limiter.acquire()
            page_data = await self.api_client.get_collect_aweme(
                str(collects_id), max_cursor=cursor, count=20
            )

            page_items = page_data.get("aweme_list", [])
            if not page_items:
                break

            aweme_list.extend(page_items)

            has_more = bool(page_data.get("has_more", False))
            next_cursor = int(page_data.get("max_cursor", 0) or 0)
            if has_more and next_cursor == cursor:
                logger.warning("Collect folder %s cursor did not advance", collects_id)
                break
            cursor = next_cursor

        result.total = len(aweme_list)
        logger.info("Found %d videos in collection %s", result.total, collects_id)

        # 下载每个视频
        async def _process_aweme(item: Dict[str, Any]):
            aweme_id = item.get("aweme_id")
            if not aweme_id:
                return {"status": "failed", "aweme_id": None}

            # 收藏夹下载使用 "collect" 模式
            author_name = "collect"
            success = await self._download_aweme_assets(item, author_name, mode="collect")
            status = "success" if success else "failed"
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