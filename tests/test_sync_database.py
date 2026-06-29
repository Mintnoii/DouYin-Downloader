import pytest
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime

from storage.database import Database


@pytest.mark.asyncio
async def test_sync_history_table_creation():
    """测试sync_history表是否正确创建"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        # 检查表是否存在
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_history'"
        )
        row = await cursor.fetchone()
        assert row is not None, "sync_history表未创建"

        # 检查表结构
        cursor = await db.execute("PRAGMA table_info(sync_history)")
        columns = [row[1] for row in await cursor.fetchall()]
        expected_columns = ['id', 'sync_id', 'started_at', 'completed_at', 'status',
                          'total_videos', 'new_videos', 'processed_videos', 'failed_videos',
                          'error_message', 'config', 'created_at']
        for col in expected_columns:
            assert col in columns, f"列 {col} 不存在"

    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_sync_history_operations():
    """测试sync_history表的增删改查操作"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        # 创建同步历史记录
        sync_data = {
            "status": "pending",
            "config": {"mode": "incremental"}
        }
        sync_id = await db.create_sync_history(sync_data)
        assert sync_id is not None, "sync_id应该生成"

        # 更新同步记录
        await db.update_sync_history(sync_id, {
            "status": "completed",
            "total_videos": 100,
            "processed_videos": 95,
            "failed_videos": 5
        })

        # 查询同步历史
        history = await db.get_sync_history(limit=1)
        assert len(history) == 1, "应该有一条记录"
        assert history[0]['sync_id'] == sync_id
        assert history[0]['status'] == "completed"
        assert history[0]['total_videos'] == 100

    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_video_processing_status_table_creation():
    """测试video_processing_status表是否正确创建"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        # 检查表是否存在
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='video_processing_status'"
        )
        row = await cursor.fetchone()
        assert row is not None, "video_processing_status表未创建"

        # 检查表结构
        cursor = await db.execute("PRAGMA table_info(video_processing_status)")
        columns = [row[1] for row in await cursor.fetchall()]
        expected_columns = ['id', 'sync_id', 'aweme_id', 'status', 'file_path',
                          'transcript_path', 'error_message', 'created_at', 'updated_at']
        for col in expected_columns:
            assert col in columns, f"列 {col} 不存在"

    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_video_processing_status_operations():
    """测试video_processing_status表的增删改查操作"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        # 创建同步记录
        sync_data = {"status": "running"}
        sync_id = await db.create_sync_history(sync_data)

        # 创建视频处理状态
        video_data = {
            "aweme_id": "123456789",
            "file_path": "/path/to/video.mp4"
        }
        video_id = await db.create_video_processing_status(sync_id, video_data)
        assert video_id is not None, "video_id应该生成"

        # 更新视频处理状态
        await db.update_video_processing_status(sync_id, "123456789", {
            "status": "completed",
            "transcript_path": "/path/to/transcript.txt"
        })

        # 查询视频处理状态
        statuses = await db.get_video_processing_status(sync_id)
        assert len(statuses) == 1, "应该有一条记录"
        assert statuses[0]['aweme_id'] == "123456789"
        assert statuses[0]['status'] == "completed"
        assert statuses[0]['transcript_path'] == "/path/to/transcript.txt"

        # 测试状态统计
        stats = await db.get_sync_statistics(sync_id)
        assert stats["completed"] == 1
        assert stats["pending"] == 0

    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_foreign_key_constraint():
    """测试外键约束"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        db = Database(tmp_path)
        await db.initialize()

        # 不存在的sync_id
        video_data = {
            "aweme_id": "123456789",
            "file_path": "/path/to/video.mp4"
        }

        # 应该能够创建（没有外键约束，因为sqlite默认是关闭的）
        video_id = await db.create_video_processing_status("nonexistent_sync_id", video_data)
        assert video_id is not None

    finally:
        Path(tmp_path).unlink(missing_ok=True)