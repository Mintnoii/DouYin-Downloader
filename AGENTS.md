<!-- 生成于：2026-03-27 | 更新于：2026-05-08 -->

# douyin-downloader

## 用途
一个基于 Python 的抖音（TikTok 中国）批量下载器，可获取视频、图集、音乐和用户内容，支持无水印下载。支持多种下载模式（用户发布、点赞、合集、音乐）、并发下载、速率限制、基于 Cookie 的身份验证，以及可选的 Whisper 语音转录。通过 YAML 配置文件驱动的命令行工具。

## 关键文件

| 文件 | 描述 |
|------|-------------|
| `run.py` | 入口点 — 引导 `sys.path` 并委托给 `cli.main:main()` |
| `__init__.py` | 包版本号（`2.0.0`） |
| `pyproject.toml` | 构建配置、依赖项、CLI 入口点（`douyin-dl`）、工具设置 |
| `config.example.yml` | 示例 YAML 配置文件，供用户复制和自定义 |
| `requirements.txt` | 锁定的依赖项列表（与 pyproject.toml 一致） |
| `Dockerfile` | 下载器的容器构建文件 |
| `PROJECT_SUMMARY.md` | 架构概览文档 |

## 子目录

| 目录 | 用途 |
|-----------|---------|
| `auth/` | Cookie 和 MS Token 管理（见 `auth/AGENTS_CN.md`） |
| `cli/` | CLI 参数解析、主异步循环、进度显示（见 `cli/AGENTS_CN.md`） |
| `config/` | YAML 配置加载、环境变量覆盖、默认值（见 `config/AGENTS_CN.md`） |
| `control/` | 并发控制 — 速率限制器、重试处理器、队列管理器（见 `control/AGENTS_CN.md`） |
| `core/` | 业务逻辑 — API 客户端、URL 解析器、下载器、策略模式（见 `core/AGENTS_CN.md`） |
| `storage/` | SQLite 数据库、文件管理、元数据处理（见 `storage/AGENTS_CN.md`） |
| `tests/` | 包含 23 个测试模块的 Pytest 测试套件（见 `tests/AGENTS_CN.md`） |
| `tools/` | 独立工具，如基于浏览器的 cookie 获取（见 `tools/AGENTS_CN.md`） |
| `utils/` | 共享辅助工具 — 日志、验证、反机器人签名（见 `utils/AGENTS_CN.md`） |

## 面向 AI 代理

### 在此目录中工作
- 需要 Python 3.8+ 兼容性 — 避免使用海象运算符、`match` 语句和 `type` 别名
- 所有 I/O 都是异步的（`aiohttp`、`aiofiles`、`aiosqlite`）— 核心路径中永远不要使用阻塞 I/O
- 入口点是 `cli.main:main()`，它调用 `asyncio.run(main_async(args))`
- 配置基于 YAML，支持环境变量覆盖（`DOUYIN_*` 前缀）
- `mix`/`allmix` 配置别名系统需要特殊处理（见 `config/config_loader.py`）

### 与桌面应用的共享逻辑
- 此项目与 `/Users/crimson/codes/douyin/douyin-downloader-desktop` 共享 Python 后端逻辑
- 在修复 `auth/`、`cli/`、`config/`、`control/`、`core/`、`storage/`、`tools/`、`utils/` 或共享测试中的共享逻辑时，在两个项目中应用等效的修复，除非差异明确是仅桌面端或仅 CLI 的
- **同步脚本：** `../douyin-downloader-desktop/scripts/sync-to-cli.sh` 将所有共享文件从桌面项目复制到这里。运行 `--check` 可检测差异
- **有意的不一致之处**（这些文件按设计不同）：
  - `cli/main.py` — CLI 省略了桌面专有的 `_verify_self_checksum()` 和 `_enforce_license_at_startup()`
  - `run.py` — CLI 是简单的引导程序；桌面端有 sidecar 启动 + 数据目录迁移
  - `server/app.py`、`server/jobs.py` — CLI 服务器是简化版本；桌面端添加了许可证、SSE、覆盖和取消功能
  - `control/__init__.py` — CLI 不导出 `ProgressReporter` 类（仅供桌面 UI 使用）

### 测试要求
- 运行：`python -m pytest tests/`
- 异步测试使用 `pytest-asyncio`，配置为 `asyncio_mode = "auto"`
- 代码检查：`ruff check .`（目标 Python 3.8，行长度 100）

### 常见模式
- 下载器的工厂模式（`DownloaderFactory.create()`）
- 用户下载模式的策略模式（`core/user_modes/`）
- 模式发现的注册表模式（`UserModeRegistry`）
- 所有下载器都继承自 `BaseDownloader` 并共享 `_download_mode_items()`
- 通过 `utils.logger.setup_logger(name)` 进行模块化日志记录

## 依赖项

### 外部依赖
- `aiohttp` — 用于 API 调用和下载的异步 HTTP 客户端
- `aiofiles` — 异步文件 I/O
- `aiosqlite` — 异步 SQLite 用于下载历史
- `rich` — 终端 UI（进度条、表格、样式化输出）
- `pyyaml` — YAML 配置解析
- `python-dateutil` — 用于时间范围过滤的日期/时间解析
- `gmssl` — 用于反机器人签名的中国 SM3/SM4 加密

### 可选依赖
- `playwright` — 用于 cookie 获取的浏览器自动化
- `openai-whisper` — 音频转录

<!-- 手动： -->