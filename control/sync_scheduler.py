"""
定时同步调度器
基于cron表达式实现定时任务调度，支持自动触发收藏夹同步
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Callable

from croniter import croniter
from utils.logger import setup_logger

from .sync_manager import SyncManager

logger = setup_logger("SyncScheduler")


class SyncScheduler:
    """定时同步调度器"""

    def __init__(self, sync_manager: SyncManager, config: Dict[str, Any]):
        self.sync_manager = sync_manager
        self.config = config
        self.sync_config = config.get("sync", {})

        # 定时任务相关
        self._cron_expression = self.sync_config.get("cron_expression", "0 0 */6 * *")
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # 任务队列
        self._sync_queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []

        # 回调函数
        self._sync_callback: Optional[Callable] = None

        # 定时器
        self._next_run_time: Optional[datetime] = None
        self._timer_task: Optional[asyncio.Task] = None

    def start(self):
        """启动调度器"""
        if self._is_running:
            logger.warning("Scheduler is already running")
            return

        logger.info(f"Starting sync scheduler with cron: {self._cron_expression}")
        self._is_running = True
        self._stop_event.clear()

        # 启动调度器
        self._task = asyncio.create_task(self._run_scheduler())

        # 启动同步工作线程
        num_workers = self.config.get("thread", 4)
        for i in range(num_workers):
            worker = asyncio.create_task(self._sync_worker(f"worker-{i}"))
            self._workers.append(worker)

        # 如果启用启动时同步
        if self.sync_config.get("sync_on_startup", False):
            logger.info("Sync on startup enabled, triggering immediate sync")
            asyncio.create_task(self.schedule_sync(reason="startup"))

    async def stop(self):
        """停止调度器"""
        if not self._is_running:
            return

        logger.info("Stopping sync scheduler")
        self._is_running = False
        self._stop_event.set()

        # 停止定时器
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass

        # 停止主任务
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # 停止工作线程
        for worker in self._workers:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        self._workers.clear()
        logger.info("Sync scheduler stopped")

    def schedule_sync(self, reason: str = "manual"):
        """调度一次同步任务"""
        try:
            sync_id = f"manual_{int(datetime.now().timestamp())}"
            logger.info(f"Scheduling sync ({reason}): {sync_id}")

            # 将任务放入队列
            self._sync_queue.put_nowait({
                "sync_id": sync_id,
                "reason": reason,
                "scheduled_at": datetime.now().isoformat()
            })

            return sync_id
        except asyncio.QueueFull:
            logger.error("Sync queue is full")
            return None

    def set_sync_callback(self, callback: Callable):
        """设置同步完成回调函数"""
        self._sync_callback = callback

    async def _run_scheduler(self):
        """运行调度器主循环"""
        try:
            # 计算下次运行时间
            self._next_run_time = self._calculate_next_run_time()

            while self._is_running:
                try:
                    # 等待下次运行时间或停止事件
                    if self._next_run_time:
                        delay = (self._next_run_time - datetime.now()).total_seconds()
                        if delay > 0:
                            self._timer_task = asyncio.create_task(asyncio.sleep(delay))
                            await asyncio.wait_for(
                                self._timer_task,
                                timeout=delay
                            )
                            self._timer_task = None

                    # 触发定时同步
                    if self._is_running:
                        sync_id = self.schedule_sync("scheduled")
                        if sync_id:
                            logger.info(f"Triggered scheduled sync: {sync_id}")

                    # 更新下次运行时间
                    self._next_run_time = self._calculate_next_run_time()

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in scheduler: {str(e)}")
                    # 出错后等待一段时间再重试
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("Scheduler cancelled")
        except Exception as e:
            logger.error(f"Scheduler error: {str(e)}", exc_info=True)
        finally:
            self._is_running = False

    def _calculate_next_run_time(self) -> Optional[datetime]:
        """计算下次运行时间"""
        try:
            if not self._cron_expression or self._cron_expression == "never":
                return None

            # 使用croniter计算下次运行时间
            cron = croniter(self._cron_expression, datetime.now())
            next_time = cron.get_next(datetime)

            logger.debug(f"Next sync scheduled at: {next_time}")
            return next_time

        except Exception as e:
            logger.error(f"Error calculating next run time: {str(e)}")
            return None

    async def _sync_worker(self, worker_name: str):
        """同步工作线程"""
        logger.info(f"Starting sync worker: {worker_name}")

        while self._is_running:
            try:
                # 从队列获取任务
                task_data = await self._sync_queue.get()
                logger.info(f"{worker_name} processing sync: {task_data['sync_id']}")

                try:
                    # 执行同步
                    result = await self.sync_manager.sync_collects()

                    # 调用回调函数
                    if self._sync_callback:
                        try:
                            await self._sync_callback(task_data, result)
                        except Exception as e:
                            logger.error(f"Error in sync callback: {str(e)}")

                    logger.info(f"{worker_name} completed sync: {task_data['sync_id']}")

                except Exception as e:
                    logger.error(f"{worker_name} failed sync: {task_data['sync_id']}, error: {str(e)}")

                finally:
                    # 标记任务完成
                    self._sync_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{worker_name} error: {str(e)}")
                # 出错后等待一段时间再重试
                await asyncio.sleep(10)

        logger.info(f"Sync worker stopped: {worker_name}")

    async def get_status(self) -> Dict[str, Any]:
        """获取调度器状态"""
        return {
            "is_running": self._is_running,
            "cron_expression": self._cron_expression,
            "next_run_time": self._next_run_time.isoformat() if self._next_run_time else None,
            "queue_size": self._sync_queue.qsize(),
            "active_workers": len([w for w in self._workers if not w.done()]),
            "total_workers": len(self._workers)
        }

    async def get_sync_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取同步历史"""
        return await self.sync_manager.get_sync_history(limit)

    def get_cron_examples(self) -> Dict[str, str]:
        """获取常用cron表达式示例"""
        return {
            "every_6_hours": "0 0 */6 * *",
            "every_12_hours": "0 0 */12 * *",
            "every_day": "0 0 0 * *",
            "every_week": "0 0 0 * 0",
            "every_month": "0 0 1 1 *",
            "hourly": "0 * * * *",
            "every_30_minutes": "*/30 * * * *",
            "every_5_minutes": "*/5 * * * *"
        }

    def validate_cron_expression(self, expression: str) -> bool:
        """验证cron表达式"""
        try:
            croniter(expression, datetime.now())
            return True
        except Exception:
            return False