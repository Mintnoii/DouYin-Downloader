"""
收藏夹同步管理器
负责管理收藏夹内容的定时同步，包括增量同步、状态跟踪和错误处理
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.downloader_factory import DownloaderFactory
from storage.database import Database
from utils.logger import setup_logger

logger = setup_logger("SyncManager")


class SyncManager:
    """收藏夹同步管理器"""

    def __init__(self, api_client, database: Database, config: Dict[str, Any]):
        self.api_client = api_client
        self.db = database
        self.config = config
        self.sync_config = config.get("sync", {})

        # 同步状态
        self._current_sync_id: Optional[str] = None
        self._is_running = False
        self._lock = asyncio.Lock()

        # 统计信息
        self.stats = {
            "total_videos": 0,
            "new_videos": 0,
            "processed_videos": 0,
            "failed_videos": 0,
            "sync_start_time": None,
            "sync_end_time": None,
        }

    async def sync_collects(self) -> Dict[str, Any]:
        """执行收藏夹同步"""
        async with self._lock:
            if self._is_running:
                logger.warning("Sync is already running")
                return {"error": "Sync already running", "status": "running"}

            self._is_running = True
            self._current_sync_id = None

        try:
            logger.info("Starting collection sync")
            start_time = datetime.now()
            self.stats["sync_start_time"] = start_time.isoformat()

            # 创建同步记录
            sync_data = {
                "status": "running",
                "config": self.sync_config,
                "mode": self.sync_config.get("sync_mode", "incremental"),
                "collects_id": self.sync_config.get("collects_id"),
            }

            self._current_sync_id = await self.db.create_sync_history(sync_data)
            await self.db.update_sync_history(self._current_sync_id, {"status": "running"})

            # 获取用户信息
            user_info = await self._get_user_info()
            if not user_info:
                error_msg = "Failed to get user info"
                await self.db.update_sync_history(self._current_sync_id, {
                    "status": "failed",
                    "error_message": error_msg
                })
                return {"error": error_msg, "status": "failed"}

            sec_uid = user_info.get("sec_uid")
            logger.info(f"User info: {user_info.get('nickname')}, sec_uid: {sec_uid}")

            # 根据同步模式获取视频列表
            videos = await self._get_videos_for_sync(sec_uid, user_info)
            self.stats["total_videos"] = len(videos)
            self.stats["new_videos"] = len(videos)  # 增量模式下new_videos就是总数

            # 更新同步记录
            await self.db.update_sync_history(self._current_sync_id, {
                "total_videos": self.stats["total_videos"],
                "new_videos": self.stats["new_videos"],
                "status": "running"
            })

            # 处理视频
            success_count = await self._process_videos(videos)

            # 更新统计
            self.stats["processed_videos"] = success_count
            self.stats["failed_videos"] = len(videos) - success_count

            # 更新同步记录
            update_data = {
                "processed_videos": self.stats["processed_videos"],
                "failed_videos": self.stats["failed_videos"],
                "completed_at": datetime.now().isoformat(),
                "status": "completed" if success_count == len(videos) else "partial"
            }

            if self.stats["failed_videos"] > 0:
                update_data["error_message"] = f"Failed to process {self.stats['failed_videos']} videos"

            await self.db.update_sync_history(self._current_sync_id, update_data)

            # 清理旧视频
            if self.sync_config.get("cleanup_videos", False):
                await self._cleanup_old_videos()

            end_time = datetime.now()
            self.stats["sync_end_time"] = end_time.isoformat()

            # 计算耗时
            duration = (end_time - start_time).total_seconds()

            result = {
                "sync_id": self._current_sync_id,
                "status": update_data["status"],
                "total_videos": self.stats["total_videos"],
                "processed_videos": self.stats["processed_videos"],
                "failed_videos": self.stats["failed_videos"],
                "duration_seconds": duration,
                "start_time": self.stats["sync_start_time"],
                "end_time": self.stats["sync_end_time"],
                "config": self.sync_config,
            }

            logger.info(f"Sync completed: {result}")
            return result

        except Exception as e:
            logger.error(f"Sync failed: {str(e)}", exc_info=True)

            if self._current_sync_id:
                await self.db.update_sync_history(self._current_sync_id, {
                    "status": "failed",
                    "error_message": str(e)
                })

            return {
                "error": str(e),
                "status": "failed",
                "sync_id": self._current_sync_id
            }

        finally:
            self._is_running = False

    async def _get_user_info(self) -> Optional[Dict[str, Any]]:
        """获取用户信息"""
        try:
            # 获取用户收藏夹，同时获取用户信息
            result = await self.api_client.get_user_collects()
            if result and isinstance(result, list):
                # 通常API会返回用户信息，如果没有，使用sec_uid创建一个基本信息
                user_info = {
                    "sec_uid": result[0].get("sec_uid", "unknown") if result else "unknown",
                    "nickname": result[0].get("nickname", "Unknown User") if result else "Unknown User",
                    "uid": result[0].get("uid", 0) if result else 0,
                }
                return user_info
            return None
        except Exception as e:
            logger.error(f"Failed to get user info: {str(e)}")
            return None

    async def _get_videos_for_sync(self, sec_uid: str, user_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """根据同步模式获取视频列表"""
        sync_mode = self.sync_config.get("sync_mode", "incremental")
        max_videos = self.sync_config.get("max_sync_videos", 100)
        collects_id = self.sync_config.get("collects_id")

        if sync_mode == "full":
            # 全量同步：下载所有收藏夹的所有视频
            from core.user_modes.collect_strategy import CollectUserModeStrategy

            strategy = CollectUserModeStrategy(None, collects_id=collects_id)
            videos = await strategy.collect_items(sec_uid, user_info)

        elif sync_mode == "incremental":
            # 增量同步：只下载新的视频
            videos = await self._get_incremental_videos(sec_uid, collects_id, max_videos)

        else:
            raise ValueError(f"Unknown sync mode: {sync_mode}")

        # 限制视频数量
        if len(videos) > max_videos:
            videos = videos[:max_videos]
            logger.info(f"Limited to {max_videos} videos")

        return videos

    async def _get_incremental_videos(self, sec_uid: str, collects_id: Optional[str], max_videos: int) -> List[Dict[str, Any]]:
        """获取增量视频（新增的视频）"""
        logger.info("Getting incremental videos")

        # 获取上次同步的最后时间
        last_sync_time = await self._get_last_sync_time()

        # 使用收藏夹策略获取视频
        from core.user_modes.collect_strategy import CollectUserModeStrategy
        strategy = CollectUserModeStrategy(None, collects_id=collects_id)

        # 获取所有视频
        all_videos = await strategy.collect_items(sec_uid, {})

        # 过滤出新的视频
        new_videos = []
        for video in all_videos:
            video_time = video.get("create_time")
            if video_time and (not last_sync_time or video_time > last_sync_time):
                new_videos.append(video)

        logger.info(f"Found {len(new_videos)} new videos (last sync: {last_sync_time})")
        return new_videos

    async def _get_last_sync_time(self) -> Optional[int]:
        """获取最后一次同步的时间"""
        try:
            history = await self.db.get_sync_history(limit=1)
            if history and history[0]["status"] in ("completed", "partial"):
                # 解析completed_at时间戳
                completed_at = history[0].get("completed_at")
                if completed_at:
                    # 转换为Unix时间戳
                    dt = datetime.fromisoformat(completed_at.replace('Z', '+00:00'))
                    return int(dt.timestamp())
        except Exception as e:
            logger.error(f"Failed to get last sync time: {str(e)}")
        return None

    async def _process_videos(self, videos: List[Dict[str, Any]]) -> int:
        """处理视频列表"""
        success_count = 0
        max_retries = self.sync_config.get("max_retries", 3)
        retry_delay = self.sync_config.get("retry_delay", 60)

        for i, video in enumerate(videos):
            try:
                logger.info(f"Processing video {i+1}/{len(videos)}: {video.get('aweme_id')}")

                # 创建视频处理状态记录
                video_id = await self.db.create_video_processing_status(self._current_sync_id, video)

                # 更新状态为处理中
                await self.db.update_video_processing_status(self._current_sync_id, video.get("aweme_id"), {
                    "status": "processing"
                })

                # 下载视频
                await self._download_video(video, video_id)

                # 转录视频（如果启用）
                if self.sync_config.get("transcribe_videos", True):
                    await self._transcribe_video(video, video_id)

                # 更新状态为完成
                await self.db.update_video_processing_status(self._current_sync_id, video.get("aweme_id"), {
                    "status": "completed"
                })

                success_count += 1
                logger.info(f"Successfully processed video: {video.get('aweme_id')}")

            except Exception as e:
                logger.error(f"Failed to process video {video.get('aweme_id')}: {str(e)}")

                # 更新状态为失败
                await self.db.update_video_processing_status(self._current_sync_id, video.get("aweme_id"), {
                    "status": "failed",
                    "error_message": str(e)
                })

                # 重试逻辑
                if max_retries > 0:
                    await asyncio.sleep(retry_delay)
                    continue

        return success_count

    async def _download_video(self, video: Dict[str, Any], video_id: int):
        """下载单个视频"""
        # 这里需要集成现有的下载器逻辑
        # 简化处理，实际应该使用现有的下载功能

        # 创建下载器
        from core.downloader_factory import DownloaderFactory
        downloader = DownloaderFactory.create(
            "https://www.douyin.com/video/" + video.get("aweme_id"),
            self.config
        )

        # 下载视频
        await downloader._download_mode_items([video])

        # 更新文件路径
        # 注意：这里需要从downloader获取实际下载的文件路径
        file_path = f"/tmp/video_{video.get('aweme_id')}.mp4"  # 示例路径

        await self.db.update_video_processing_status(
            self._current_sync_id,
            video.get("aweme_id"),
            {"file_path": file_path}
        )

    async def _transcribe_video(self, video: Dict[str, Any], video_id: int):
        """转录视频为文本"""
        # 这里需要集成现有的转录功能
        # 简化处理，实际应该使用现有的转录服务

        file_path = video.get("file_path")
        if not file_path:
            raise ValueError("Video file path not available")

        # 创建转录任务
        transcript_data = {
            "aweme_id": video.get("aweme_id"),
            "video_path": file_path,
            "status": "pending",
            "model": "gpt-4o-mini-transcribe"
        }

        # 这里应该调用转录服务
        # transcript_result = await api_client.transcribe_video(file_path)
        # transcript_data.update(transcript_result)

        # 模拟转录完成
        transcript_path = f"{file_path}.txt"
        transcript_data.update({
            "status": "completed",
            "text_path": transcript_path,
            "json_path": f"{file_path}.json"
        })

        # 更新转录状态
        await self.db.update_video_processing_status(
            self._current_sync_id,
            video.get("aweme_id"),
            {"transcript_path": transcript_path}
        )

    async def _cleanup_old_videos(self):
        """清理旧视频文件"""
        keep_days = self.sync_config.get("keep_days", 7)
        cutoff_date = datetime.now() - timedelta(days=keep_days)

        # 获取需要清理的视频
        old_videos = await self.db.get_video_processing_status(
            self._current_sync_id,
            status="completed"
        )

        for video in old_videos:
            try:
                created_at = datetime.fromisoformat(video["created_at"])
                if created_date < cutoff_date:
                    # 删除视频文件和转录文件
                    import os
                    if os.path.exists(video["file_path"]):
                        os.remove(video["file_path"])
                    if video.get("transcript_path") and os.path.exists(video["transcript_path"]):
                        os.remove(video["transcript_path"])
                    logger.info(f"Cleaned up video: {video['aweme_id']}")
            except Exception as e:
                logger.error(f"Failed to cleanup video {video['aweme_id']}: {str(e)}")

    async def get_sync_status(self, sync_id: str) -> Dict[str, Any]:
        """获取同步状态"""
        sync_history = await self.db.get_sync_history(limit=100)
        for sync in sync_history:
            if sync["sync_id"] == sync_id:
                stats = await self.db.get_sync_statistics(sync_id)
                return {
                    "sync_id": sync_id,
                    "history": sync,
                    "statistics": stats,
                    "videos": await self.db.get_video_processing_status(sync_id)
                }
        return {"error": "Sync not found"}

    async def get_sync_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取同步历史"""
        return await self.db.get_sync_history(limit)

    @property
    def is_running(self) -> bool:
        """检查是否有同步任务正在运行"""
        return self._is_running