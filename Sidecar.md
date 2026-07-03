# Rust ↔ Python Sidecar 方案实践总结

## 背景

将 douyin-downloader 项目的抖音登录、收藏夹视频下载、Whisper 转录三大功能，通过 Rust（后续 Tauri）调用现有 Python 核心代码实现，避免完整重写。

## 方案设计

```
┌─────────────────────────────────────────┐
│           Rust / Tauri 端                │
│  ┌─────────────────────────────────┐    │
│  │    stdin → JSON 命令             │    │
│  │    stdout ← JSON 响应 + 进度通知  │    │
│  │    stderr ← 日志透传             │    │
│  └──────────────┬──────────────────┘    │
│                 │                        │
│  ┌──────────────▼──────────────────┐    │
│  │   Python Sidecar 子进程          │    │
│  │   sidecar_main.py               │    │
│  │   复用现有 core/ auth/ config/   │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
```

### 通信协议

每行一个 JSON，stdout 通道复用。通过 `"type": "progress"` 字段区分进度通知和最终响应。

**请求：**
```json
{"id":"1","method":"ping","params":{}}
```

**最终响应：**
```json
{"id":"1","ok":true,"result":{"pong":true}}
```

**进度通知（长时间操作期间发送，可有多个）：**
```json
{"id":"3","type":"progress","step":"download_start","detail":"开始下载收藏夹 xxx"}
```

### 关键技术决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 进程通信方式 | stdin/stdout 行协议 | 比 HTTP 少一层网络开销，不需要端口管理，进程管理更干净 |
| 输出编码 | `sys.stdout.buffer.write(utf-8)` | Windows 上 `sys.stdout.write()` 默认使用终端编码（GBK），中文字符会导致 Rust 端 UTF-8 解析失败 |
| 输入编码 | `sys.stdin.buffer` + `.decode("utf-8")` | Windows 上 `sys.stdin` 默认按 GBK 解码，Rust 写入的 UTF-8 中文路径会变成乱码 |
| stdin 读取 | 独立线程同步阻塞读 | `asyncio connect_read_pipe` 在 Windows subprocess pipe 场景 EOF 检测有问题 |
| JSON 解析 | Rust 端手写简易解析器 | 避免 cargo 依赖下载问题（第一次 cargo build 卡在 crates.io 索引更新），`rustc` 直接编译零依赖 |
| CUDA 初始化 | sidecar 启动时预加载 `ctranslate2` | 长期运行的 asyncio 进程中首次 import ctranslate2 会导致 CUDA 驱动初始化卡死，必须在事件循环启动前同步加载 |

## 项目结构

```
douyin-downloader/                    # 现有项目
├── sidecar_main.py                   # ★ 新增：Python IPC 入口（~450行）
│
douyin-downloader-sidecar-poc/        # ★ 新建：Rust PoC
├── src/main.rs                       # Rust 调用端（~280行，零外部依赖）
├── Cargo.toml
└── poc.exe                           # rustc 编译产物
```

### sidecar_main.py 支持的方法

| 方法 | 功能 | 状态 |
|------|------|------|
| `ping` | 连通性检测 | ✅ |
| `list_collections` | 列出当前账号所有收藏夹（ID + 名称 + 作品数） | ✅ |
| `download_collection` | 下载指定收藏夹视频（支持 `max_count` 限制） | ✅ |
| `transcribe` | 对单个视频文件进行 Whisper 转录 | ✅ |
| `shutdown` | 优雅退出 | ✅ |

### Rust 端核心逻辑

1. `Command::new("python").arg("sidecar_main.py").stdin/stdout/stderr` 启动子进程
2. `call(method, params)` → 写 stdin 一行 JSON → 循环读 stdout 行
3. 读到 `"type":"progress"` 的行 → 打印进度给用户
4. 读到含 `"ok"` 的行 → 最终响应，返回
5. Drop 时自动发送 `shutdown`

## 踩坑记录

### 1. Windows stdout 编码

**现象：** Rust 端 `read_line` 报 `InvalidData: stream did not contain valid UTF-8`

**原因：** Windows 上 `sys.stdout.write()` 默认使用终端编码（GBK/CP936），`json.dumps(ensure_ascii=False)` 输出的中文字符被编码为非 UTF-8 字节。

**修复：** 改用 `sys.stdout.buffer.write(data.encode("utf-8"))` 显式 UTF-8 编码，绕开终端编码。

### 2. Windows stdin 编码（中文路径乱码）

**现象：** Rust 通过 stdin 传入的中文文件路径在 Python 侧变成乱码（如 `护肤邪修` → `鎶よ偆閭�淇�`），导致 ffmpeg 报 `Error opening input: No such file or directory`。

**原因：** 与 stdout 同理但方向相反——Windows 上 `sys.stdin` 默认按 GBK/CP936 解码文本。Rust 写入的 UTF-8 字节在 Python 侧被错误解码。

**修复：** `_reader_thread` 改用 `sys.stdin.buffer` 读取原始字节，再 `.decode("utf-8")`：

```python
for line_bytes in sys.stdin.buffer:          # 读原始字节
    line_str = line_bytes.decode("utf-8")    # 显式 UTF-8 解码
```

### 3. Python json.dumps 空格问题

**现象：** Rust 端 JSON 解析器找不到 `"result":{` 或 `"collections":[`

**原因：** Python `json.dumps()` 在 `:` 和 `,` 后自动加空格（`"ok": true`、`"result": {"collections": [...]`），而解析器匹配的是无空格版本。

**修复：** 所有 JSON 字段提取函数先尝试 `"key": `（带空格），失败再尝试 `"key":`（无空格）。

### 4. asyncio stdin pipe EOF 检测

**现象：** 使用 `connect_read_pipe` + `StreamReader.readline()` 在 Windows subprocess pipe 关闭后不返回空行，进程挂死。

**修复：** 改用独立线程 `for line in sys.stdin:` 同步阻塞读，通过 `asyncio.Queue` 与事件循环通信。

### 5. 下载步骤无进度输出

**现象：** 调用 `download_collection` 后长时间无任何输出，看起来像卡死了。

**原因：**
- `CollectDownloader.download()` 完全没有调用 progress reporter，分页拉取和逐项下载过程中静默执行
- `file_manager.py` 的下载失败日志为 `logger.debug` 级别，控制台默认 `ERROR` 级别看不到
- Python 模块的 logger 默认 `console_level=ERROR`，大量 INFO/WARNING 日志被静默丢弃

**修复：**
- `CollectDownloader.download()` 加入完整的进度报告：`_progress_update_step`、`_progress_set_item_total`、`_progress_advance_item`
- `sidecar_main.py` 中实现 `_SidecarProgressReporter` 桥接到 `_notify()`，传入 `DownloaderFactory.create()`
- sidecar 启动时以及关键模块导入后调用 `set_console_log_level(logging.INFO)` 提升日志可见性
- `file_manager.py` 下载失败日志改为 `logger.warning` 并输出 HTTP 状态码 + 响应体截断

### 6. ffmpeg 中文路径编码

**现象：** 音频提取时 ffmpeg 报 `Error opening input: No such file or directory`，路径中中文变成乱码。

**原因：** Windows 上 `str(Path)` 传给子进程时，参数编码使用系统 ANSI 代码页，中文文件名被破坏。

**修复：** `audio_extraction.py` 中使用 `\\\\?\\` 前缀的 Unicode 路径：

```python
if os.name == "nt":
    if not _input.startswith("\\\\?\\"):
        _input = "\\\\?\\" + _input
```

### 7. 下载路径双重嵌套

**现象：** 下载的文件保存在 `Downloaded/collect/collect/xxx.mp4`，多了一层无意义的 `collect/`。

**原因：** `author_name="collect"` + `mode="collect"` + `group_by_mode=True` 三重叠加。

**修复：** sidecar 配置中加 `group_by_mode=False`，路径变为 `Downloaded/collect/xxx.mp4`。

### 8. CUDA 在 asyncio 进程中初始化卡死

**现象：** 音频提取完成后，`import ctranslate2` 卡住无响应，任务管理器中 GPU 内存占用无变化。但单独运行 Python 脚本秒过。

**原因：** 长期运行的 asyncio 事件循环进程中，首次 `import ctranslate2`（触发 CUDA 驱动初始化）会导致死锁。这与事件循环、信号处理、或已存在的线程状态有关。

**修复：** sidecar 启动时、事件循环开始前第一时间同步预加载 CUDA：

```python
# 必须在 asyncio.run() 之前执行
import ctranslate2
```

同时提供环境变量 `DOUYIN_SKIP_CUDA_PRELOAD=1` 作为逃生舱。

### 9. 设备信息不可见

**现象：** 转录时无从判断使用的是 CPU 还是 GPU。

**修复：** 转录开始日志中显式输出设备信息：

- CUDA 可用: `🎙 转录 xxx (0.5MB, medium, GPU(CUDA))`
- CPU 回退: `🎙 转录 xxx (0.5MB, medium, CPU(int8))`

模型复用/加载时也输出 `device=xxx, compute=xxx`。

## 验证结果

```
Step 1: ping                                    ✅ 连通正常
Step 2: list_collections                        ✅ 成功拉取 20 个收藏夹（中文名称正常显示）
Step 3: download_collection (限 1 个)            ✅ 视频下载 + 音频提取 + Whisper GPU 转录全链路通
Step 4: transcribe (独立转录)                    ✅ 中文路径正确传递，转录文件正常生成
```

全链路输出示例：

```
▼ 下载完成 2026-05-23_护肤邪修..._7642669161081137070
🔊 提取音频 2026-05-23_护肤邪修..._7642669161081137070
🔊 提取完成 1.0s 2026-05-23_护肤邪修..._7642669161081137070
🎙 转录 2026-05-23_护肤邪修..._7642669161081137070 (0.5MB, medium, GPU(CUDA))
[whisper] 获取模型锁...
[whisper] ctranslate2 4.8.0, CUDA设备数=1
✅ Whisper model loaded in 7.5s: medium (device=cuda, compute=float16)
▶ 开始 faster-whisper 推理（VAD + 识别）...
✅ Whisper 转录完成: 12 segments, 语言=zh, 总耗时 8.2s
```

## 对比纯 Rust 重写

| 维度 | 纯 Rust 重写 | Sidecar 方案 |
|------|-------------|-------------|
| 开发时间 | 8-12 周 | 2-3 周 |
| XBogus 签名 | 需完整重写（~300行纯算法） | 复用现有 Python 实现 |
| Whisper 转录 | 需接入 whisper-rs/candle | 复用 faster-whisper + CUDA |
| 测试 | 所有测试需重写 | 现有 test_*.py 全部保留 |
| Cookie 管理 | 需重写 | 复用现有逻辑 |
| 代码量 | 全新项目 | Rust ~280 行 + Python ~450 行（入口层） |
| 分发体积 | ~30MB（Tauri + Rust） | ~120MB（Tauri + embedded Python + 依赖） |

## 后续 Tauri 集成

当前 PoC 的 stdin/stdout 行协议可直接用于 Tauri sidecar 模式：

1. 将 Python + 依赖打包为 embedded Python 放入 Tauri `binaries/` 目录
2. Rust 端用 `tauri::api::process::Command::new_sidecar("python")` 管理子进程
3. 前端通过 `invoke` 调用 Rust 命令 → Rust 转发给 Python → 返回结果
4. 进度通知通过 Tauri event 推送到前端实时展示
5. sidecar 启动时自动预加载 CUDA，确保转录流程不卡顿

PoC 已验证核心 IPC 链路可行，Tauri 集成是纯工程工作。
