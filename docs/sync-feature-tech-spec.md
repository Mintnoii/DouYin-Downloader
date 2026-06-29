# 抖音收藏夹定时同步功能技术方案

## 1. 概述

基于现有的抖音下载器架构，实现收藏夹内容的定时同步功能，自动检测并处理新增收藏的视频，转录为文本后清理视频文件。

## 2. 架构设计

### 2.1 整体架构

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   定时调度器     │    │   同步管理器     │    │   API客户端     │
│  SyncScheduler   │    │ SyncManager     │    │ APIClient       │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         └───────────┬───────────┼───────────┬───────────┘
                     │           │           │
            ┌─────────────────────────────────────────────┐
            │             数据库层                       │
            │  ┌─────────────┐  ┌─────────────┐        │
            │  │sync_history │  │video_proce  │        │
            │  │  表         │  │ssing_status │        │
            │  └─────────────┘  └─────────────┘        │
            └─────────────────────────────────────────────┘
                     │           │           │
            ┌─────────────────────────────────────────────┐
            │            存储层                         │
            │  ┌─────────────┐  ┌─────────────┐        │
            │  │   视频文件   │  │   文本文件   │        │
            │  └─────────────┘  └─────────────┘        │
            └─────────────────────────────────────────────┘
```

### 2.2 核心组件

#### 2.2.1 SyncScheduler (定时调度器)
- **职责**：基于cron表达式调度同步任务
- **功能**：
  - 解析cron表达式
  - 触发同步任务
  - 管理同步任务状态

#### 2.2.2 SyncManager (同步管理器)
- **职责**：管理整个同步流程
- **功能**：
  - 同步任务执行
  - 增量同步逻辑
  - 状态跟踪
  - 错误处理和重试

#### 2.2.3 数据库扩展
- **sync_history**：记录同步历史
- **video_processing_status**：跟踪视频处理状态

## 3. 数据库设计

### 3.1 sync_history 表

```sql
CREATE TABLE sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id TEXT NOT NULL UNIQUE,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
    total_videos INTEGER DEFAULT 0,
    new_videos INTEGER DEFAULT 0,
    processed_videos INTEGER DEFAULT 0,
    failed_videos INTEGER DEFAULT 0,
    error_message TEXT,
    config TEXT,  -- JSON格式存储同步配置
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sync_history_status ON sync_history(status);
CREATE INDEX idx_sync_history_started_at ON sync_history(started_at);
```

### 3.2 video_processing_status 表

```sql
CREATE TABLE video_processing_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id TEXT NOT NULL,
    aweme_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'completed', 'failed')),
    file_path TEXT,
    transcript_path TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sync_id) REFERENCES sync_history(sync_id)
);

CREATE INDEX idx_video_processing_status_sync_id ON video_processing_status(sync_id);
CREATE INDEX idx_video_processing_status_aweme_id ON video_processing_status(aweme_id);
CREATE INDEX idx_video_processing_status_status ON video_processing_status(status);
```

## 4. 配置扩展

### 4.1 新增配置项

```yaml
# config/default_config.py
SYNC_CONFIG = {
    # 定时同步配置
    "enabled": False,  # 是否启用定时同步
    "cron_expression": "0 0 */6 * *",  # 每6小时同步一次
    "sync_on_startup": False,  # 启动时是否执行同步
    
    # 同步策略配置
    "sync_mode": "incremental",  # 同步模式：incremental（增量）| full（全量）
    "collects_id": None,  # 指定收藏夹ID，None表示同步所有收藏夹
    "max_sync_videos": 100,  # 单次同步最大视频数
    
    # 处理配置
    "transcribe_videos": True,  # 是否转录视频
    "cleanup_videos": True,  # 处理完成后是否清理视频文件
    "keep_days": 7,  # 保留视频文件的天数
    
    # 重试配置
    "max_retries": 3,
    "retry_delay": 60,  # 重试延迟（秒）
}

# CLI参数支持
cli.add_argument("--sync", action="store_true", help="执行收藏夹同步")
cli.add_argument("--sync-once", action="store_true", help="执行一次同步后退出")
```

## 5. 实现计划

### 5.1 第一阶段：数据库扩展
1. 实现数据库表结构的创建和迁移
2. 添加数据库操作接口
3. 实现同步状态跟踪

### 5.2 第二阶段：同步管理器
1. 实现SyncManager类
2. 添加增量同步逻辑
3. 实现同步状态管理

### 5.3 第三阶段：定时调度器
1. 实现SyncScheduler类
2. 添加cron表达式解析
3. 实现任务调度和触发

### 5.4 第四阶段：集成和测试
1. 集成到现有CLI
2. 添加服务模式支持
3. 完善测试用例

## 6. API设计

### 6.1 SyncManager接口

```python
class SyncManager:
    async def sync_collects(self) -> Dict[str, Any]:
        """执行收藏夹同步"""
        
    async def get_sync_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取同步历史记录"""
        
    async def get_sync_status(self, sync_id: str) -> Optional[Dict[str, Any]]:
        """获取同步状态"""
```

### 6.2 SyncScheduler接口

```python
class SyncScheduler:
    def __init__(self, sync_manager: SyncManager, config: Dict[str, Any]):
        ...
        
    async def start(self):
        """启动定时任务"""
        
    async def stop(self):
        """停止定时任务"""
        
    def schedule_sync(self):
        """手动触发同步"""
```

## 7. 关键算法

### 7.1 增量同步算法

```python
async def incremental_sync(self, last_sync_id: str) -> List[Dict[str, Any]]:
    """
    增量同步逻辑：
    1. 获取上一次同步的最后一个aweme_id
    2. 从该位置开始获取新的视频
    3. 避免重复下载
    """
    # 获取上一次同步的最后一个aweme_id
    last_video = await self.get_last_synced_video(last_sync_id)
    
    # 从API获取新的视频
    new_videos = await self.fetch_new_videos_after(last_video)
    
    # 过滤掉已处理的视频
    unprocessed_videos = [
        video for video in new_videos
        if not await self.is_video_processed(video['aweme_id'])
    ]
    
    return unprocessed_videos
```

### 7.2 状态管理

```python
class SyncStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
```

## 8. 错误处理

### 8.1 网络错误处理
- API请求失败自动重试
- 网络超时处理
- 频率限制检测

### 8.2 数据错误处理
- 数据库连接失败重试
- 事务回滚
- 数据一致性保证

## 9. 性能考虑

### 9.1 内存管理
- 分页加载大列表
- 流式处理视频数据
- 及时释放资源

### 9.2 并发控制
- 使用信号量控制并发
- 避免数据库连接泄露
- 合理设置超时时间

## 10. 日志和监控

### 10.1 日志设计
```python
logger = {
    "sync": {
        "level": "INFO",
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    },
    "video": {
        "level": "INFO",
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    }
}
```

### 10.2 监控指标
- 同步成功率
- 平均处理时间
- 错误率统计
- 资源使用情况

## 11. 扩展性设计

### 11.1 插件机制
- 支持自定义同步策略
- 支持自定义处理器
- 支持自定义通知方式

### 11.2 配置热更新
- 支持运行时更新配置
- 支持平滑重启
- 支持配置版本管理