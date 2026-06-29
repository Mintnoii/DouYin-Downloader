import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from control.sync_scheduler import SyncScheduler
from control.sync_manager import SyncManager


@pytest.mark.asyncio
async def test_sync_scheduler_initialization():
    """测试SyncScheduler初始化"""
    mock_sync_manager = MagicMock()
    config = {
        "sync": {
            "enabled": True,
            "cron_expression": "0 0 */6 * *",
            "sync_on_startup": False
        }
    }

    scheduler = SyncScheduler(mock_sync_manager, config)

    assert scheduler.sync_manager == mock_sync_manager
    assert scheduler.config == config
    assert not scheduler._is_running
    assert scheduler._task is None
    assert scheduler._next_run_time is None


@pytest.mark.asyncio
async def test_sync_scheduler_start_stop():
    """测试启动和停止调度器"""
    mock_sync_manager = AsyncMock()
    config = {"sync": {"enabled": True}}

    scheduler = SyncScheduler(mock_sync_manager, config)

    # 启动调度器
    scheduler.start()
    assert scheduler._is_running
    assert scheduler._task is not None

    # 停止调度器
    await scheduler.stop()
    assert not scheduler._is_running


@pytest.mark.asyncio
async def test_sync_scheduler_schedule_sync():
    """测试调度同步任务"""
    mock_sync_manager = AsyncMock()
    config = {"sync": {}}

    scheduler = SyncScheduler(mock_sync_manager, config)

    # 调度一次同步
    sync_id = scheduler.schedule_sync(reason="test")
    assert sync_id is not None
    assert sync_id.startswith("test_")

    # 再次调度
    sync_id2 = scheduler.schedule_sync(reason="manual")
    assert sync_id2 is not None
    assert sync_id2.startswith("manual_")


@pytest.mark.asyncio
async def test_sync_scheduler_next_run_time():
    """测试计算下次运行时间"""
    mock_sync_manager = AsyncMock()
    config = {
        "sync": {
            "cron_expression": "0 * * * *",  # 每小时执行
        }
    }

    scheduler = SyncScheduler(mock_sync_manager, config)

    # 计算下次运行时间
    next_time = scheduler._calculate_next_run_time()
    assert next_time is not None
    assert next_time > datetime.now()

    # 测试无效的cron表达式
    config["sync"]["cron_expression"] = "invalid"
    next_time = scheduler._calculate_next_run_time()
    assert next_time is None


@pytest.mark.asyncio
async def test_sync_scheduler_get_status():
    """测试获取调度器状态"""
    mock_sync_manager = AsyncMock()
    config = {"sync": {}}

    scheduler = SyncScheduler(mock_sync_manager, config)

    # 获取初始状态
    status = await scheduler.get_status()
    assert status["is_running"] == False
    assert status["queue_size"] == 0
    assert status["active_workers"] == 0
    assert status["total_workers"] == 0

    # 启动后
    scheduler.start()
    status = await scheduler.get_status()
    assert status["is_running"] == True
    assert status["total_workers"] > 0


@pytest.mark.asyncio
async def test_sync_scheduler_get_cron_examples():
    """测试获取cron表达式示例"""
    mock_sync_manager = AsyncMock()
    config = {"sync": {}}

    scheduler = SyncScheduler(mock_sync_manager, config)

    examples = scheduler.get_cron_examples()
    assert "every_6_hours" in examples
    assert "every_day" in examples
    assert "every_week" in examples
    assert "hourly" in examples


@pytest.mark.asyncio
async def test_sync_scheduler_validate_cron_expression():
    """测试验证cron表达式"""
    mock_sync_manager = AsyncMock()
    config = {"sync": {}}

    scheduler = SyncScheduler(mock_sync_manager, config)

    # 有效的cron表达式
    assert scheduler.validate_cron_expression("0 * * * *") == True
    assert scheduler.validate_cron_expression("0 0 * * *") == True

    # 无效的cron表达式
    assert scheduler.validate_cron_expression("invalid") == False
    assert scheduler.validate_cron_expression("60 * * * *") == False  # 分钟不能是60


@pytest.mark.asyncio
async def test_sync_scheduler_sync_on_startup():
    """测试启动时同步"""
    mock_sync_manager = AsyncMock()
    config = {
        "sync": {
            "enabled": True,
            "sync_on_startup": True,
        }
    }

    scheduler = SyncScheduler(mock_sync_manager, config)

    # 启动调度器
    scheduler.start()

    # 验证触发了同步
    await asyncio.sleep(0.1)  # 等待一点时间让任务开始

    # 检查队列中有任务
    status = await scheduler.get_status()
    assert status["queue_size"] > 0

    # 停止调度器
    await scheduler.stop()


@pytest.mark.asyncio
async def test_sync_worker_error_handling():
    """测试工作线程错误处理"""
    mock_sync_manager = AsyncMock()
    # 模拟同步失败
    mock_sync_manager.sync_collects.side_effect = Exception("Test error")

    config = {"sync": {}}
    scheduler = SyncScheduler(mock_sync_manager, config)

    # 设置回调函数来捕获错误
    error_log = []
    def error_callback(task_data, result):
        error_log.append((task_data, result))

    scheduler.set_sync_callback(error_callback)

    # 启动调度器
    scheduler.start()

    # 调度同步任务
    sync_id = scheduler.schedule_sync(reason="error_test")

    # 等待任务完成
    await asyncio.sleep(0.5)

    # 验证错误被记录
    assert len(error_log) > 0
    assert error_log[0][1]["status"] == "failed"
    assert error_log[0][1]["error"] == "Test error"

    # 停止调度器
    await scheduler.stop()