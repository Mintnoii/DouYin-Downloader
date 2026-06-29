import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import tempfile

from control.sync_manager import SyncManager
from storage.database import Database


@pytest.mark.asyncio
async def test_sync_manager_initialization():
    """测试SyncManager初始化"""
    mock_api_client = MagicMock()
    mock_db = MagicMock()

    config = {
        "sync": {
            "enabled": True,
            "sync_mode": "incremental",
            "cron_expression": "0 0 */6 * *",
            "max_sync_videos": 100,
            "transcribe_videos": True,
            "cleanup_videos": True,
            "keep_days": 7
        }
    }

    sync_manager = SyncManager(mock_api_client, mock_db, config)

    assert sync_manager.api_client == mock_api_client
    assert sync_manager.db == mock_db
    assert sync_manager.sync_config == config["sync"]
    assert not sync_manager._is_running
    assert sync_manager._current_sync_id is None


@pytest.mark.asyncio
async def test_sync_manager_get_user_info():
    """测试获取用户信息"""
    mock_api_client = AsyncMock()
    mock_db = MagicMock()

    # 模拟API返回数据
    mock_api_client.get_user_collects.return_value = [
        {
            "sec_uid": "test_sec_uid",
            "nickname": "Test User",
            "uid": 12345
        }
    ]

    config = {"sync": {}}
    sync_manager = SyncManager(mock_api_client, mock_db, config)

    user_info = await sync_manager._get_user_info()

    assert user_info is not None
    assert user_info["sec_uid"] == "test_sec_uid"
    assert user_info["nickname"] == "Test User"
    assert user_info["uid"] == 12345


@pytest.mark.asyncio
async def test_sync_manager_get_user_info_empty():
    """测试获取用户信息（空返回）"""
    mock_api_client = AsyncMock()
    mock_db = MagicMock()

    # 模拟API返回空数据
    mock_api_client.get_user_collects.return_value = []

    config = {"sync": {}}
    sync_manager = SyncManager(mock_api_client, mock_db, config)

    user_info = await sync_manager._get_user_info()

    # 空数据时应该返回默认值
    assert user_info is not None
    assert user_info["sec_uid"] == "unknown"


@pytest.mark.asyncio
async def test_sync_manager_get_last_sync_time():
    """测试获取上次同步时间"""
    mock_api_client = AsyncMock()

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        config = {"sync": {}}
        sync_manager = SyncManager(mock_api_client, db, config)

        # 初始时没有同步记录
        last_sync_time = await sync_manager._get_last_sync_time()
        assert last_sync_time is None

        # 创建一条完成的同步记录
        sync_data = {
            "status": "completed",
            "completed_at": "2024-01-01T12:00:00"
        }
        sync_id = await db.create_sync_history(sync_data)
        await db.update_sync_history(sync_id, sync_data)

        # 应该能获取到上次同步时间
        last_sync_time = await sync_manager._get_last_sync_time()
        assert last_sync_time == 1704110400  # 2024-01-01T12:00:00 的Unix时间戳

    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_sync_manager_is_running():
    """测试同步运行状态"""
    mock_api_client = AsyncMock()
    mock_db = MagicMock()

    config = {"sync": {}}
    sync_manager = SyncManager(mock_api_client, mock_db, config)

    # 初始状态
    assert not sync_manager.is_running

    # 模拟运行中状态
    sync_manager._is_running = True
    assert sync_manager.is_running

    # 模拟停止
    sync_manager._is_running = False
    assert not sync_manager.is_running


@pytest.mark.asyncio
async def test_sync_manager_get_sync_status():
    """测试获取同步状态"""
    mock_api_client = AsyncMock()

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        config = {"sync": {}}
        sync_manager = SyncManager(mock_api_client, db, config)

        # 创建同步记录
        sync_data = {"status": "completed"}
        sync_id = await db.create_sync_history(sync_data)
        await db.update_sync_history(sync_id, sync_data)

        # 创建视频处理状态
        video_data = {
            "aweme_id": "123456",
            "file_path": "/path/to/video.mp4"
        }
        await db.create_video_processing_status(sync_id, video_data)

        # 获取同步状态
        status = await sync_manager.get_sync_status(sync_id)

        assert status["sync_id"] == sync_id
        assert "history" in status
        assert "statistics" in status
        assert "videos" in status
        assert len(status["videos"]) == 1

        # 测试不存在的sync_id
        not_found = await sync_manager.get_sync_status("nonexistent")
        assert "error" in not_found

    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_sync_manager_get_sync_history():
    """测试获取同步历史"""
    mock_api_client = AsyncMock()

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        config = {"sync": {}}
        sync_manager = SyncManager(mock_api_client, db, config)

        # 创建几条同步记录
        for i in range(3):
            sync_data = {"status": "completed"}
            sync_id = await db.create_sync_history(sync_data)
            await db.update_sync_history(sync_id, sync_data)

        # 获取同步历史
        history = await sync_manager.get_sync_history(limit=2)

        assert len(history) == 2
        assert all("sync_id" in sync for sync in history)
        assert all("status" in sync for sync in history)

    finally:
        Path(tmp_path).unlink(missing_ok=True)