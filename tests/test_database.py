import asyncio
import json

import pytest

from storage import Database


@pytest.mark.asyncio
async def test_database_aweme_lifecycle(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))

    await database.initialize()

    aweme_payload = {
        "aweme_id": "123",
        "aweme_type": "video",
        "title": "test",
        "author_id": "author",
        "author_name": "Author",
        "create_time": 1700000000,
        "file_path": "/tmp",
        "metadata": json.dumps({"a": 1}, ensure_ascii=False),
    }

    await database.add_aweme(aweme_payload)

    assert await database.is_downloaded("123") is True
    assert await database.get_aweme_count_by_author("author") == 1
    assert await database.get_latest_aweme_time("author") == 1700000000

    await database.add_history(
        {
            "url": "https://www.douyin.com/video/123",
            "url_type": "video",
            "total_count": 1,
            "success_count": 1,
            "config": json.dumps({"path": "./Downloaded/"}, ensure_ascii=False),
        }
    )

    await database.close()


@pytest.mark.asyncio
async def test_database_transcript_job_upsert(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    await database.upsert_transcript_job(
        {
            "aweme_id": "123",
            "video_path": "/tmp/demo.mp4",
            "transcript_dir": "/tmp",
            "text_path": "/tmp/demo.transcript.txt",
            "json_path": "/tmp/demo.transcript.json",
            "model": "gpt-4o-mini-transcribe",
            "status": "skipped",
            "skip_reason": "missing_api_key",
            "error_message": None,
        }
    )

    row = await database.get_transcript_job("123")
    assert row is not None
    assert row["status"] == "skipped"
    assert row["skip_reason"] == "missing_api_key"

    await database.upsert_transcript_job(
        {
            "aweme_id": "123",
            "video_path": "/tmp/demo.mp4",
            "transcript_dir": "/tmp",
            "text_path": "/tmp/demo.transcript.txt",
            "json_path": "/tmp/demo.transcript.json",
            "model": "gpt-4o-mini-transcribe",
            "status": "success",
            "skip_reason": None,
            "error_message": None,
        }
    )

    row = await database.get_transcript_job("123")
    assert row["status"] == "success"
    assert row["skip_reason"] is None

    await database.close()


@pytest.mark.asyncio
async def test_database_initialize_sets_wal_journal_mode(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    db = await database._get_conn()
    cursor = await db.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"

    cursor = await db.execute("PRAGMA synchronous")
    row = await cursor.fetchone()
    # synchronous=NORMAL == 1
    assert row is not None
    assert int(row[0]) == 1

    await database.close()


@pytest.mark.asyncio
async def test_add_aweme_batch_inserts_all_items(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    items = [
        {
            "aweme_id": str(i),
            "aweme_type": "video",
            "title": f"title-{i}",
            "author_id": "author",
            "author_name": "Author",
            "create_time": 1700000000 + i,
            "file_path": "/tmp",
            "metadata": json.dumps({"i": i}, ensure_ascii=False),
        }
        for i in range(100)
    ]

    await database.add_aweme_batch(items)

    assert await database.get_aweme_count_by_author("author") == 100
    for i in range(100):
        assert await database.is_downloaded(str(i)) is True

    await database.close()


@pytest.mark.asyncio
async def test_add_aweme_batch_empty_list_is_noop(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    await database.add_aweme_batch([])

    assert await database.get_aweme_count_by_author("author") == 0

    await database.close()


@pytest.mark.asyncio
async def test_add_aweme_batch_replaces_on_conflict(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    base = {
        "aweme_id": "777",
        "aweme_type": "video",
        "title": "first",
        "author_id": "author",
        "author_name": "Author",
        "create_time": 1700000000,
        "file_path": "/tmp/a",
        "metadata": json.dumps({"v": 1}, ensure_ascii=False),
    }
    await database.add_aweme_batch([base])

    updated = dict(base)
    updated["title"] = "second"
    updated["file_path"] = "/tmp/b"
    await database.add_aweme_batch([updated])

    db = await database._get_conn()
    cursor = await db.execute("SELECT title, file_path FROM aweme WHERE aweme_id = ?", ("777",))
    row = await cursor.fetchone()
    assert row == ("second", "/tmp/b")

    cursor = await db.execute("SELECT COUNT(*) FROM aweme WHERE aweme_id = ?", ("777",))
    count_row = await cursor.fetchone()
    assert count_row[0] == 1

    await database.close()


@pytest.mark.asyncio
async def test_add_aweme_batch_uses_single_commit(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    db = await database._get_conn()
    commit_count = {"n": 0}
    original_commit = db.commit

    async def counting_commit():
        commit_count["n"] += 1
        return await original_commit()

    monkeypatch.setattr(db, "commit", counting_commit)

    items = [
        {
            "aweme_id": str(i),
            "aweme_type": "video",
            "title": f"t{i}",
            "author_id": "a",
            "author_name": "A",
            "create_time": 1700000000 + i,
            "file_path": "/tmp",
            "metadata": "{}",
        }
        for i in range(50)
    ]
    await database.add_aweme_batch(items)

    assert commit_count["n"] == 1, (
        f"expected exactly 1 commit for batch insert, got {commit_count['n']}"
    )

    await database.close()


@pytest.mark.asyncio
async def test_database_get_conn_reuses_single_connection_under_concurrency(tmp_path, monkeypatch):
    import storage.database as database_module

    connect_calls = []

    class _FakeConn:
        def __init__(self, db_path: str):
            self.db_path = db_path
            self.closed = False

        async def close(self):
            self.closed = True

    async def _fake_connect(db_path: str):
        connect_calls.append(db_path)
        await asyncio.sleep(0)
        return _FakeConn(db_path)

    monkeypatch.setattr(database_module.aiosqlite, "connect", _fake_connect)

    database = Database(str(tmp_path / "test.db"))
    conn_a, conn_b = await asyncio.gather(database._get_conn(), database._get_conn())

    assert conn_a is conn_b
    assert connect_calls == [str(tmp_path / "test.db")]

    await database.close()


@pytest.mark.asyncio
async def test_sync_history_table_creation(tmp_path):
    """测试 sync_history 表的创建和基本操作"""
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))

    await database.initialize()

    # 插入同步记录
    sync_data = {
        "sync_id": "sync_001",
        "url": "https://www.douyin.com/user/123",
        "url_type": "user",
        "sync_mode": "full",
        "start_time": 1700000000,
        "end_time": 1700000001,
        "total_videos": 10,
        "new_videos": 8,
        "existing_videos": 2,
        "failed_videos": 0,
        "error_message": None,
        "config": json.dumps({"path": "./Downloaded/"}, ensure_ascii=False),
    }

    await database.add_sync_history(sync_data)

    # 验证记录存在
    record = await database.get_sync_history("sync_001")
    assert record is not None
    assert record["sync_id"] == "sync_001"
    assert record["sync_mode"] == "full"
    assert record["new_videos"] == 8
    assert record["end_time"] == 1700000001

    await database.close()


@pytest.mark.asyncio
async def test_video_processing_status_table_creation(tmp_path):
    """测试 video_processing_status 表的创建和基本操作"""
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))

    await database.initialize()

    # 插入视频处理状态记录
    processing_data = {
        "aweme_id": "video_001",
        "aweme_title": "测试视频",
        "author_id": "author_123",
        "author_name": "测试作者",
        "file_path": "/tmp/video_001.mp4",
        "file_size": 1024000,
        "duration": 30,
        "format": "mp4",
        "quality": "720p",
        "processing_status": "pending",
        "processing_started_at": None,
        "processing_completed_at": None,
        "processing_duration": None,
        "error_message": None,
        "retry_count": 0,
        "metadata": json.dumps({"resolution": "1280x720"}, ensure_ascii=False),
    }

    await database.add_video_processing_status(processing_data)

    # 验证记录存在
    record = await database.get_video_processing_status("video_001")
    assert record is not None
    assert record["aweme_id"] == "video_001"
    assert record["processing_status"] == "pending"
    assert record["author_name"] == "测试作者"

    # 更新处理状态
    updated_data = processing_data.copy()
    updated_data["processing_status"] = "completed"
    updated_data["processing_started_at"] = 1700000002
    updated_data["processing_completed_at"] = 1700000005
    updated_data["processing_duration"] = 3

    await database.update_video_processing_status(updated_data)

    # 验证更新
    updated_record = await database.get_video_processing_status("video_001")
    assert updated_record["processing_status"] == "completed"
    assert updated_record["processing_duration"] == 3

    await database.close()


@pytest.mark.asyncio
async def test_sync_history_pagination(tmp_path):
    """测试 sync_history 表的分页功能"""
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))

    await database.initialize()

    # 插入多条同步记录
    for i in range(5):
        sync_data = {
            "sync_id": f"sync_{i:03d}",
            "url": "https://www.douyin.com/user/123",
            "url_type": "user",
            "sync_mode": "full",
            "start_time": 1700000000 + i,
            "end_time": 1700000001 + i,
            "total_videos": 10 + i,
            "new_videos": 8 + i,
            "existing_videos": 2,
            "failed_videos": 0,
            "error_message": None,
            "config": json.dumps({"path": "./Downloaded/"}, ensure_ascii=False),
        }
        await database.add_sync_history(sync_data)

    # 测试分页查询
    page1 = await database.get_sync_history_list(page=1, size=2)
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert page1["items"][0]["sync_id"] == "sync_004"  # 按时间倒序

    page2 = await database.get_sync_history_list(page=2, size=2)
    assert len(page2["items"]) == 2
    assert page2["items"][0]["sync_id"] == "sync_002"

    await database.close()


@pytest.mark.asyncio
async def test_video_processing_status_by_author(tmp_path):
    """测试按作者查询视频处理状态"""
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))

    await database.initialize()

    # 插入不同作者的视频处理记录
    for i in range(3):
        processing_data = {
            "aweme_id": f"video_{i:03d}",
            "aweme_title": f"视频{i}",
            "author_id": f"author_{i%2}",  # 两个作者
            "author_name": f"作者{i%2}",
            "file_path": f"/tmp/video_{i:03d}.mp4",
            "file_size": 1024000 + i * 1000,
            "duration": 30 + i,
            "format": "mp4",
            "quality": "720p",
            "processing_status": "completed" if i % 2 == 0 else "pending",
            "processing_started_at": 1700000002 + i,
            "processing_completed_at": 1700000005 + i if i % 2 == 0 else None,
            "processing_duration": 3 + i if i % 2 == 0 else None,
            "error_message": None,
            "retry_count": 0,
            "metadata": json.dumps({"resolution": "1280x720"}, ensure_ascii=False),
        }
        await database.add_video_processing_status(processing_data)

    # 按作者查询
    author0_videos = await database.get_video_processing_status_by_author("author_0")
    assert len(author0_videos) == 2  # author_0 有 2 个视频
    assert all(r["author_id"] == "author_0" for r in author0_videos)

    author1_videos = await database.get_video_processing_status_by_author("author_1")
    assert len(author1_videos) == 1  # author_1 有 1 个视频
    assert all(r["author_id"] == "author_1" for r in author1_videos)

    # 按状态查询
    completed_videos = await database.get_video_processing_status_by_status("completed")
    assert len(completed_videos) == 2
    assert all(r["processing_status"] == "completed" for r in completed_videos)

    pending_videos = await database.get_video_processing_status_by_status("pending")
    assert len(pending_videos) == 1
    assert all(r["processing_status"] == "pending" for r in pending_videos)

    await database.close()
