# TODO — 转录性能后续优化

> 以下优化项基于流水线改造（方案二）完成后，可叠加实施。

---

## 1. Whisper 模型预热

**现状**：`TranscriptManager._get_whisper_model()` 在首次转录调用时懒加载 `faster-whisper.WhisperModel`，加载 medium 模型约需 5-15 秒。

**优化**：在下载开始前（`UserDownloader.download()` 阶段）异步预加载模型，使首个视频的转录无需等待模型加载。

**位置**：`core/transcript_manager.py:478-492`、`core/user_downloader.py:58`

---

## 2. 音频提取复用

**现状**：`process_video` 每次转录都重新用 ffmpeg 提取音轨到临时文件，转录完成后清理。重试时重新提取。

**优化**：将提取的音频缓存到视频同目录（如 `.transcript_cache/{aweme_id}.mp3`），转录成功后清理，失败重试时直接复用。

**位置**：`core/transcript_manager.py:273-305`

---

## 3. 转录结果即时写 DB

**现状**：`_download_mode_items` 在所有下载+转录完成后才遍历结果写 `DownloadResult` 统计。大批次下内存中积累大量 `_transcript_results`。

**优化**：消费者转录完成后立即写入 `transcript_jobs` 表（`_record_job` 已在 `process_video` 内部调用），统计时从 DB 读取而非内存 dict。

**位置**：`core/user_downloader.py:331-355`、`core/transcript_manager.py:561-589`

---

## 4. OpenAI 后端多消费者并行

**现状**：流水线改造后使用 1 个转录消费者。本地 Whisper 受 `_whisper_lock` 限制必须串行，但 OpenAI API 后端没有此限制，可以并行发送多个请求。

**优化**：根据 `transcript.backend` 配置动态设置消费者数量：
- `backend: whisper` → 1 个消费者（CTranslate2 非线程安全）
- `backend: openai` → 可配置数量（如 `transcript.openai_concurrency`，默认 3）

**位置**：`core/user_downloader.py` `_download_mode_items` 中消费者创建逻辑

---

## 5. CollectDownloader / MixDownloader 转录结果利用

**现状**：`CollectDownloader` 和 `MixDownloader` 也调用 `_download_aweme_assets`（会触发转录），但它们的 `_process_aweme` 闭包和结果统计逻辑不读取 `_transcript_results`，转录结果被静默丢弃——白白消耗了 CPU/API 调用。

**优化**：要么让它们也消费转录结果（加统计），要么给它们传 `transcribe=False` 彻底跳过转录。

**位置**：`core/collect_downloader.py:50-59`、`core/mix_downloader.py:34-47`

---

## 6. 大批次转录队列内存优化

**现状**：`asyncio.Queue` 无界。在极端大批次（数千视频）场景下，下载速度远快于转录速度时队列可能堆积大量（video_path, aweme_id）元组。

**优化**：给队列设置 `maxsize`（如 `thread * 2`），利用背压（backpressure）自然限速——当队列满时下载 worker 在 `put()` 处等待，间接防止下载与转录差距过大。

**位置**：`core/user_downloader.py` `_download_mode_items`
