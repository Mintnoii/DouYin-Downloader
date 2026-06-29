# 抖音收藏夹定时同步功能

## 功能概述

本功能实现了抖音收藏夹的定时同步，可以自动检测并处理新增收藏的视频，支持转录为文本后清理视频文件。

## 使用方法

### 1. 基本同步

执行一次同步：
```bash
python run.py --sync-once
```

### 2. 启动定时同步

启动定时同步服务：
```bash
python run.py --sync
```

使用自定义cron表达式：
```bash
python run.py --sync --sync-cron "0 0 */3 * *"
```

### 3. 配置文件

在 `config.yml` 中配置同步参数：

```yaml
sync:
  # 是否启用定时同步
  enabled: true
  # cron表达式（默认每6小时同步一次）
  cron_expression: "0 0 */6 * *"
  # 启动时是否执行同步
  sync_on_startup: false
  # 同步模式：incremental（增量）| full（全量）
  sync_mode: "incremental"
  # 指定收藏夹ID，None表示同步所有收藏夹
  collects_id: null
  # 单次同步最大视频数
  max_sync_videos: 100
  # 是否转录视频
  transcribe_videos: true
  # 处理完成后是否清理视频文件
  cleanup_videos: true
  # 保留视频文件的天数
  keep_days: 7
  # 重试配置
  max_retries: 3
  retry_delay: 60  # 重试延迟（秒）
```

### 4. 常用cron表达式

| 表达式 | 说明 |
|--------|------|
| `0 0 */6 * *` | 每6小时 |
| `0 0 */12 * *` | 每12小时 |
| `0 0 0 * *` | 每天午夜 |
| `0 0 0 * 0` | 每周日午夜 |
| `0 0 1 1 *` | 每月1日午夜 |
| `0 * * * *` | 每小时 |
| `*/30 * * * *` | 每30分钟 |
| `*/5 * * * *` | 每5分钟 |

## 功能特性

### 1. 双同步模式

- **增量同步**（默认）：只下载新增的视频，基于视频的创建时间
- **全量同步**：下载所有收藏夹的视频

### 2. 状态跟踪

- 记录每次同步的详细信息
- 跟踪每个视频的处理状态
- 支持查看同步历史和统计数据

### 3. 文本转录

- 自动转录视频内容
- 支持多种输出格式（TXT、JSON）
- 保留转录文本供后续使用

### 4. 自动清理

- 处理完成后可选择清理视频文件
- 基于保留策略自动清理旧文件
- 保留转录文本文件

### 5. 错误处理

- 自动重试失败的下载
- 详细的错误日志记录
- 支持手动重新触发同步

## 数据库

### 新增数据表

1. **sync_history** - 同步历史记录
2. **video_processing_status** - 视频处理状态

### 查看同步历史

```python
from storage.database import Database

db = Database("dy_downloader.db")
await db.initialize()

# 获取最近10条同步记录
history = await db.get_sync_history(limit=10)
for sync in history:
    print(f"Sync ID: {sync['sync_id']}")
    print(f"Status: {sync['status']}")
    print(f"Videos: {sync['total_videos']}")
    print(f"Processed: {sync['processed_videos']}")
    print("-" * 40)
```

## API参考

### SyncManager

```python
from control.sync_manager import SyncManager

# 创建同步管理器
sync_manager = SyncManager(api_client, database, config)

# 执行同步
result = await sync_manager.sync_collects()

# 获取同步状态
status = await sync_manager.get_sync_status(sync_id)
```

### SyncScheduler

```python
from control.sync_scheduler import SyncScheduler

# 创建调度器
scheduler = SyncScheduler(sync_manager, config)

# 启动调度器
scheduler.start()

# 停止调度器
await scheduler.stop()

# 手动触发同步
sync_id = scheduler.schedule_sync(reason="manual")
```

## 故障排查

### 1. 常见错误

- **认证失败**：确保Cookie有效，可使用 `python tools/cookie_fetcher.py` 重新获取
- **网络错误**：检查网络连接，尝试调整 `retry_times` 和 `rate_limit`
- **磁盘空间不足**：清理旧的下载文件，调整 `keep_days` 设置

### 2. 日志查看

同步相关的日志会记录在：
- 控制台输出
- 日志文件：`logs/sync.log`

### 3. 手动重试

查看同步历史后，可以手动重试失败的同步：

```python
# 查看最近的同步
history = await db.get_sync_history(limit=5)

# 获取失败的同步
failed_sync = next(sync for sync in history if sync['status'] == 'failed')

# 重新执行
result = await sync_manager.sync_collects()
```

## 开发

### 运行测试

```bash
# 运行所有测试
python -m pytest tests/

# 运行同步相关测试
python -m pytest tests/test_sync_*.py

# 使用覆盖率测试
python -m pytest tests/ --cov=control.sync --cov-report=html
```

### 代码质量

```bash
# 检查代码
ruff check .

# 格式化代码
ruff format .

# 类型检查
mypy control/
```

## 扩展功能

### 1. 自定义同步策略

可以通过继承 `SyncManager` 来实现自定义的同步策略：

```python
class CustomSyncManager(SyncManager):
    async def custom_sync_logic(self):
        # 实现自定义的同步逻辑
        pass
```

### 2. 自定义处理器

可以扩展视频处理功能，添加自定义的处理步骤：

```python
async def custom_video_processor(video_data, video_id):
    # 实现自定义的视频处理逻辑
    pass
```

### 3. 自定义通知

可以通过配置添加自定义的通知方式：

```yaml
notifications:
  custom_providers:
    - type: webhook
      url: "https://your-webhook-url.com"
      method: POST
    - type: email
      smtp_server: "smtp.example.com"
      smtp_port: 587
      username: "user@example.com"
      password: "password"
      from_addr: "noreply@example.com"
      to_addr: "admin@example.com"
```