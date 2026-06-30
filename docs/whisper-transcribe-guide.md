# 视频语音转文字方案

> 2026-06-30 实测整理

## 背景

抖音视频一般不带内嵌字幕轨，需要语音转文字来获取文字稿。

项目目前已内置两条管线：

1. **云端转录**（`core/transcript_manager.py`）：下载流程中自动触发，调用 OpenAI 兼容的转录 API
2. **本地转录**（`cli/whisper_transcribe.py`）：对已下载视频批量离线转写，基于 faster-whisper

---

## 方案一：本地 Whisper（推荐，免费）

### 安装

```bash
pip install faster-whisper
```

### 使用

```bash
# 单文件
PYTHONIOENCODING=utf-8 python cli/whisper_transcribe.py -f "视频路径.mp4" -m small -l zh

# 批量目录 + SRT 字幕 + 跳过已处理
PYTHONIOENCODING=utf-8 python cli/whisper_transcribe.py -d ./Downloaded/ -m base -l zh --srt --skip-existing

# GPU 加速（需 CUDA）
python cli/whisper_transcribe.py -f "视频.mp4" -m medium --cuda
```

### 参数说明

| 参数 | 说明 |
|------|------|
| `-d, --dir` | 视频目录（默认 `./Downloaded/`） |
| `-f, --file` | 单个视频文件 |
| `-m, --model` | 模型大小：`tiny` / `base` / `small` / `medium` / `large`（默认 `base`） |
| `-l, --language` | 语言（默认 `zh`） |
| `--srt` | 同时输出 SRT 字幕文件 |
| `--skip-existing` | 跳过已有 transcript 的视频 |
| `--sc` | 繁体转简体（需 `pip install OpenCC`） |
| `-o, --output` | 输出目录（默认与视频同目录） |
| `--cuda` | 使用 GPU 加速 |

### 模型选择

| 模型 | 大小 | 推理速度（CPU） | 中文质量 |
|------|------|:--:|:--:|
| `tiny` | ~75MB | 最快 | 较差 |
| `base` | ~139MB | 快 | 一般 |
| `small` | ~460MB | ~30s/分钟音频 | 较好 |
| `medium` | ~1.5GB | 较慢 | 好 |
| `large` | ~2.9GB | 很慢 | 最好 |

### 实测数据（Windows 11, Python 3.12, CPU）

| 视频 | 模型 | 引擎 | 推理耗时 |
|------|------|------|:--:|
| 5.5MB / ~1分钟中文 | base | openai-whisper | 很快 |
| 5.5MB / ~1分钟中文 | small | openai-whisper | ~2.5 分钟 |
| 5.5MB / ~1分钟中文 | small | **faster-whisper** | **~30 秒** |

faster-whisper 比 openai-whisper 快约 5 倍（CTranslate2 + INT8 量化 vs PyTorch FP32），内存占用低约 40%。

---

## 方案二：云端转录（需 API Key）

在 `config.yml` 中配置，下载时自动触发：

```yaml
transcript:
  enabled: true
  model: gpt-4o-mini-transcribe
  response_formats:
    - txt
    - json
  api_key_env: OPENAI_API_KEY
```

### 替代 OpenAI 的 API 供应商

任何兼容 OpenAI `/v1/audio/transcriptions` 格式的供应商，只需改 `api_url` 和 `api_key`：

| 供应商 | api_url | 免费额度 | 国内访问 |
|--------|---------|:--:|:--:|
| Groq | `https://api.groq.com/openai/v1/audio/transcriptions` | 有 | 需代理 |
| 硅基流动 | `https://api.siliconflow.cn/v1/audio/transcriptions` | 有 | ✅ |

```yaml
transcript:
  enabled: true
  model: whisper-large-v3
  api_url: https://api.groq.com/openai/v1/audio/transcriptions
  api_key_env: GROQ_API_KEY
```

---

## 输出文件

转写完成后，在视频所在目录（或 `-o` 指定的目录）生成：

```
视频目录/
├── 原视频.mp4
├── xxx.transcript.txt    # 纯文本文字稿
└── xxx.transcript.srt    # SRT 字幕（需加 --srt）
```
