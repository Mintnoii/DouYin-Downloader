# CLAUDE_CN.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作时提供指导。

## 开发命令

### 运行应用程序
```bash
# 使用配置文件
python run.py -c config.yml

# 使用命令行参数
python run.py -c config.yml -u "https://www.douyin.com/video/xxx" -t 8

# 作为 REST API 服务器运行（需要安装 fastapi + uvicorn）
python run.py --serve --serve-port 8000
```

### 测试
```bash
# 运行所有测试
python -m pytest tests/

# 运行特定测试文件
python -m pytest tests/test_api_client.py

# 使用安静输出运行
python -m pytest tests/ -q

# 运行覆盖率测试（可选）
python -m pytest tests/ --cov=core --cov=auth --cov=config
```

### 代码质量
```bash
# 使用 ruff 进行代码检查
ruff check .

# 格式化代码
ruff format .

# 检查并修复代码
ruff check . --fix
```

### 环境设置
```bash
# 安装依赖
pip install -r requirements.txt

# 开发模式安装
pip install -e .[dev]

# 安装浏览器支持（用于 cookie 获取）
pip install -e .[browser]

# 安装服务器模式依赖
pip install -e .[server]
```

## 架构概览

这是一个用于批量下载抖音（TikTok 中国）内容的异步优先 Python 应用。架构采用模块化设计，职责分离清晰。

### 核心架构

1. **入口点**：`run.py` 引导环境并委托给 `cli.main:main()`
2. **CLI 层**：`cli/main.py` 处理命令行参数解析并协调整个下载流程
3. **核心业务逻辑**：`core/` 包含所有下载器、API 客户端和业务规则
4. **身份验证**：`auth/` 管理 cookies、tokens 和登录会话
5. **控制层**：`control/` 管理速率限制、重试和并发
6. **存储层**：`storage/` 处理文件 I/O 和 SQLite 数据库操作
7. **配置**：`config/` 加载和验证 YAML 配置文件

### 关键设计模式

- **工厂模式**：`DownloaderFactory.create()` 根据 URL 类型实例化适当的下载器
- **策略模式**：`core/user_modes/` 实现不同的下载策略（post、like、mix、music）
- **注册表模式**：`UserModeRegistry` 发现和管理可用的下载模式
- **异步架构**：所有 I/O 操作使用 asyncio（`aiohttp`、`aiofiles`、`aiosqlite`）

### 重要约定

- 所有下载器都继承自 `BaseDownloader` 并实现 `_download_mode_items()`
- 配置使用 YAML 并支持环境变量覆盖（`DOUYIN_*` 前缀）
- 数据库操作是异步的，使用 `aiosqlite`
- 日志基于模块，通过 `utils.logger.setup_logger(name)` 实现
- 错误处理包括认证失败时的自动重新登录

### 与桌面应用的共享逻辑

此项目与 `/Users/crimson/codes/douyin/douyin-downloader-desktop` 共享 Python 后端逻辑。在修改 `auth/`、`cli/`、`config/`、`control/`、`core/`、`storage/`、`tools/` 或 `utils/` 中的文件时：

1. 在两个项目中应用等效的更改
2. 使用 `../douyin-downloader-desktop/scripts/sync-to-cli.sh --check` 检测差异
3. 记录有意的不一致之处（CLI 省略了桌面特有的功能，如许可证强制执行）

### 配置结构

关键配置部分：
- `link`：要下载的 URL
- `mode`：下载模式（post、like、mix、music、collect、collectmix）
- `number`：每模式项目限制（0 = 无限制）
- `increase`：启用增量下载
- `thread`：并发级别
- `database`：启用 SQLite 去重
- `browser_fallback`：分页时回退到浏览器
- `transcript`：可选的 OpenAI 语音转录
- `notifications`：完成时推送通知

### 测试指南

- 测试使用 `pytest-asyncio`，配置为 `asyncio_mode = "auto"`
- 使用 `unittest.mock` 在测试中模拟外部 API
- 测试成功和失败场景
- 包含主下载流程的集成测试
- 数据库测试应使用临时的 SQLite 文件

## Cookie 获取功能

### 自动获取流程

```bash
# 推荐的自动获取方式
python -m tools.cookie_fetcher --config config.yml
```

**工作原理：**
1. **启动浏览器**：使用 Playwright 自动化 Chromium 浏览器（推荐非无头模式）
2. **导航到抖音**：打开默认抖音主页 `https://www.douyin.com/`
3. **用户登录**：等待用户在浏览器中手动完成抖音登录
4. **捕获 Cookie**：在登录成功后，自动捕获所有 `douyin.com` 域的 Cookie
5. **提取 msToken**：通过多种途径动态获取 msToken（关键动态 Token）
6. **保存配置**：将 Cookie 写入 `config/cookies.json` 并自动更新配置文件

### 实现细节

#### 1. 多层 msToken 提取策略
```python
# 优先级从高到低：
# 1. 从当前 cookies 中获取
# 2. 从请求头观察中获取（URL 查询参数）
# 3. 从 cookie 头观察中获取
# 4. 从 document.cookie 中获取
# 5. 从 localStorage/sessionStorage 中获取
# 6. 从页面 JavaScript 执行中获取
```

#### 2. 智能导航降级
- 主策略：等待 `networkidle`（所有网络请求完成）
- 降级策略：超时后使用 `domcontentloaded`（仅等待 DOM 加载完成）
- 处理用户提前按 Enter 的情况

#### 3. Cookie 过滤与清理
- **必选 Cookie**：`msToken`、`ttwid`、`odin_tt`、`passport_csrf_token`
- **建议 Cookie**：包含必选 + `sid_guard`、`sessionid`、`sid_tt`
- **辅助 Cookie**：安全相关的前缀匹配 Cookie
- **清理规则**：基于 RFC6265 标准验证 Cookie 名称和值

#### 4. 自动重登机制
- 检测到 `LoginRequiredError` 时自动触发重登
- 在非服务模式下才启用交互式重登
- 重登成功后自动更新 `CookieManager` 中的 Cookie

#### 5. 配置集成
- Cookie 自动写入指定的 `config.yml` 文件
- 支持 `config/cookies.json` 作为 Cookie 存储文件
- 与现有配置系统无缝集成

### 手动 Cookie 管理

如果不使用自动获取，可以：

1. **手动获取 Cookie**：
   - 浏览器开发者工具 → Network 标签 → 刷新页面
   - 复制请求头中的 Cookie 值

2. **更新配置文件**：
   ```yaml
   cookies:
     msToken: "手动获取的 msToken"
     ttwid: "手动获取的 ttwid"
     odin_tt: "手动获取的 odin_tt"
     passport_csrf_token: "手动获取的 passport_csrf_token"
   ```

### 注意事项

1. **浏览器选择**：推荐 Chromium（默认），Firefox 和 WebKit 也可用
2. **网络环境**：确保网络可以正常访问抖音
3. **Cookie 有效期**：通常 7-30 天，过期后需要重新获取
4. **安全考虑**：Cookie 文件权限设置为 600（仅所有者可读）
5. **多账号支持**：可以通过不同的配置文件管理多个账号的 Cookie