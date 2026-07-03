import asyncio
import json
import os
import sys
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
import aiohttp

from config import ConfigLoader
from core.audio_extraction import AudioExtractError, extract_audio
from storage import Database, FileManager
from utils.logger import setup_logger

logger = setup_logger("TranscriptManager")


# File extensions that the transcription endpoint already accepts as audio.
# When the source download is one of these we skip ``extract_audio`` and
# upload the file as-is. Lower-case keys.
_SOURCE_AUDIO_MIME = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


def _mask_api_key_local(value: str) -> str:
    """Pure mirror of ``server.app._mask_api_key`` for use inside the
    shared transcript pipeline (which can't import from desktop-only
    code). Same boundary semantics: empty → ``""``, 1-7 → all ``*``,
    >=8 → ``"<first 4>...<last 4>"``.

    Used to redact bearer tokens that might be echoed back in upstream
    error responses before they land in ``transcript_jobs.error_message``
    (Property 1 / 2).
    """
    if not value:
        return ""
    n = len(value)
    if n >= 8:
        return f"{value[:4]}...{value[-4:]}"
    return "*" * n


def resolve_api_key_with_source(
    transcript_cfg: Dict[str, Any],
) -> Tuple[str, str]:
    """Pure helper that resolves a transcription API key and reports
    where it came from.

    Used by both :class:`TranscriptManager` (during a real
    ``process_video`` call) and the desktop sidecar's
    ``POST /api/v1/transcript/test-connectivity`` endpoint, so the two
    code paths can never disagree on which credential they're using.

    Priority (first non-empty after strip wins):
      1. The environment variable named by ``api_key_env``
         (default ``OPENAI_API_KEY``).
      2. The ``api_key`` field persisted in ``settings.yml``.

    Returns:
        Tuple of (api_key, source) where ``source`` is one of
        ``"env"``, ``"settings"``, or ``"none"``.
    """
    api_key_env = str(transcript_cfg.get("api_key_env", "OPENAI_API_KEY") or "").strip()
    if api_key_env:
        env_value = os.getenv(api_key_env, "").strip()
        if env_value:
            return env_value, "env"

    settings_value = str(transcript_cfg.get("api_key", "") or "").strip()
    if settings_value:
        return settings_value, "settings"
    return "", "none"


class TranscriptManager:
    def __init__(
        self,
        config: ConfigLoader,
        file_manager: FileManager,
        database: Optional[Database] = None,
    ):
        self.config = config
        self.file_manager = file_manager
        self.database = database
        self._whisper_lock = asyncio.Lock()

    def _cfg(self) -> Dict[str, Any]:
        return self.config.get("transcript", {}) or {}

    def _enabled(self) -> bool:
        return bool(self._cfg().get("enabled", False))

    def _model(self) -> str:
        return str(self._cfg().get("model", "gpt-4o-mini-transcribe")).strip()

    def _upload_audio_only(self) -> bool:
        """``transcript.upload_audio_only`` flag (R1.14, default ``True``).

        Hidden from the Settings UI by design (R1.18); editable only via
        ``settings.yml`` or a direct ``PATCH /api/v1/settings`` call so a
        user wandering through the UI can't accidentally disable the
        bandwidth-saving path.
        """
        v = self._cfg().get("upload_audio_only", True)
        if v is None:
            return True
        return bool(v)

    def _response_formats(self) -> List[str]:
        formats = self._cfg().get("response_formats", ["txt", "json"])
        if not isinstance(formats, list):
            return ["txt", "json"]
        normalized = [str(item).strip().lower() for item in formats if str(item).strip()]
        return normalized or ["txt", "json"]

    def _backend(self) -> str:
        """转录后端：``"whisper"``（本地 faster-whisper）或 ``"openai"``（云端 API）。"""
        backend = str(self._cfg().get("backend", "whisper")).strip().lower()
        if backend not in ("whisper", "openai"):
            logger.warning("Unknown transcript backend %r, falling back to whisper", backend)
            return "whisper"
        return backend

    def _whisper_model_size(self) -> str:
        """本地 Whisper 模型大小：tiny / base / small / medium / large。"""
        return str(self._cfg().get("whisper_model", "base")).strip() or "base"

    def _whisper_device(self) -> str:
        device = str(self._cfg().get("whisper_device", "cpu")).strip().lower()
        if device not in ("cpu", "cuda"):
            return "cpu"
        return device

    def _whisper_compute_type(self) -> str:
        """faster-whisper compute_type：GPU 用 float16，CPU 用 int8。"""
        return "float16" if self._whisper_device() == "cuda" else "int8"

    def _language(self) -> Optional[str]:
        lang = str(self._cfg().get("language", "zh")).strip()
        return lang if lang else None

    def _resolve_api_key(self) -> str:
        """Resolve the API key per Requirement 5.6.

        Priority (first non-empty after strip wins):
        1. The environment variable named by ``transcript.api_key_env``
           (default ``OPENAI_API_KEY``).
        2. The ``transcript.api_key`` field persisted in ``settings.yml``.
        Falling through both returns ``""`` and the caller goes through the
        existing ``skip_reason="missing_api_key"`` branch.
        """
        api_key, _source = resolve_api_key_with_source(self._cfg())
        return api_key

    def _api_url(self) -> str:
        api_url = str(
            self._cfg().get("api_url", "https://api.openai.com/v1/audio/transcriptions")
        ).strip()
        return api_url or "https://api.openai.com/v1/audio/transcriptions"

    def resolve_output_dir(self, video_path: Path) -> Path:
        video_path = Path(video_path)
        video_dir = video_path.parent
        output_dir = str(self._cfg().get("output_dir", "")).strip()
        if not output_dir:
            return video_dir

        output_root = Path(output_dir)
        try:
            relative_dir = video_dir.resolve().relative_to(self.file_manager.base_path.resolve())
            return output_root / relative_dir
        except Exception:
            logger.warning(
                "Failed to mirror transcript path for video %s, fallback to video dir",
                video_path,
            )
            return video_dir

    def _all_outputs_exist(self, text_path: Path, json_path: Path) -> bool:
        """检查所有需要的转录输出文件是否已存在。"""
        formats = set(self._response_formats())
        checks = []
        if "txt" in formats:
            checks.append(text_path.is_file())
        if "json" in formats:
            checks.append(json_path.is_file())
        return all(checks) if checks else True

    def _get_opencc(self):
        """Lazy-load OpenCC 繁体→简体转换器。"""
        if hasattr(self, "_opencc_converter") and self._opencc_converter is not None:
            return self._opencc_converter
        try:
            from opencc import OpenCC
            self._opencc_converter = OpenCC("t2s")
            logger.info("OpenCC t2s converter loaded")
        except ImportError:
            logger.warning("OpenCC not installed, skipping t→s conversion")
            self._opencc_converter = False
        return self._opencc_converter

    @staticmethod
    def _simplify_text(payload: Dict[str, Any], converter) -> Dict[str, Any]:
        """将 payload 中的 text 和 segments 从繁体转为简体。"""
        payload["text"] = converter.convert(payload.get("text", ""))
        for seg in payload.get("segments", []):
            if "text" in seg:
                seg["text"] = converter.convert(seg["text"])
        return payload

    def build_output_paths(self, video_path: Path) -> Tuple[Path, Path]:
        video_path = Path(video_path)
        output_dir = self.resolve_output_dir(video_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = video_path.stem
        return (
            output_dir / f"{stem}.transcript.txt",
            output_dir / f"{stem}.transcript.json",
        )

    async def process_video(self, video_path: Path, aweme_id: str) -> Dict[str, Any]:
        video_path = Path(video_path)

        if not self._enabled():
            return {"status": "skipped", "reason": "disabled"}

        backend = self._backend()
        text_path, json_path = self.build_output_paths(video_path)
        model = self._model() if backend == "openai" else self._whisper_model_size()

        # ── 增量检测：已有转录稿则跳过 ──────────────────────────────
        if self._all_outputs_exist(text_path, json_path):
            logger.info("Transcript already exists for aweme %s, skipping", aweme_id)
            return {"status": "skipped", "reason": "already_exists"}

        # 本地 Whisper 不需要 API key；云端 OpenAI 需要
        if backend == "openai":
            api_key = self._resolve_api_key()
            if not api_key:
                await self._record_job(
                    aweme_id=aweme_id,
                    video_path=video_path,
                    transcript_dir=text_path.parent,
                    text_path=text_path,
                    json_path=json_path,
                    model=model,
                    status="skipped",
                    skip_reason="missing_api_key",
                    error_message=None,
                )
                logger.warning("Transcript skipped for aweme %s: missing_api_key", aweme_id)
                return {"status": "skipped", "reason": "missing_api_key"}

        # ── 音频提取 ──────────────────────────────────────────────
        source_ext = video_path.suffix.lower()
        is_source_audio = source_ext in _SOURCE_AUDIO_MIME
        tmp_audio_dir: Optional[tempfile.TemporaryDirectory] = None

        upload_path = video_path
        upload_filename = video_path.name
        upload_content_type = self._guess_video_content_type(video_path)

        try:
            if not is_source_audio and self._upload_audio_only():
                logger.info("🔊 提取音频 %s", video_path.stem)
                t_extract = time.monotonic()
                tmp_audio_dir = tempfile.TemporaryDirectory(
                    prefix="transcript_audio_"
                )
                try:
                    upload_path = await extract_audio(
                        video_path, Path(tmp_audio_dir.name)
                    )
                    elapsed_extract = round(time.monotonic() - t_extract, 1)
                    logger.info("🔊 提取完成 %.1fs %s", elapsed_extract, video_path.stem)
                except AudioExtractError as exc:
                    error_message = str(exc)
                    elapsed = round(time.monotonic() - t_extract, 1)
                    await self._record_job(
                        aweme_id=aweme_id,
                        video_path=video_path,
                        transcript_dir=text_path.parent,
                        text_path=text_path,
                        json_path=json_path,
                        model=model,
                        status="failed",
                        skip_reason=None,
                        error_message=error_message,
                    )
                    logger.error(
                        "Transcript audio extraction failed for aweme %s: %s",
                        aweme_id,
                        error_message,
                    )
                    return {
                        "status": "failed",
                        "reason": "audio_extract_failed",
                        "error": error_message,
                        "duration": elapsed,
                    }
                upload_filename = f"{video_path.stem}.mp3"
                upload_content_type = "audio/mpeg"
            elif is_source_audio:
                upload_filename = video_path.name
                upload_content_type = _SOURCE_AUDIO_MIME[source_ext]

            # ── 转录 ──────────────────────────────────────────────
            t0 = time.monotonic()
            try:
                if backend == "whisper":
                    payload = await self._call_whisper_transcription(
                        file_path=upload_path,
                        model_size=model,
                    )
                else:
                    payload = await self._call_openai_transcription(
                        api_key=api_key,
                        file_path=upload_path,
                        filename=upload_filename,
                        content_type=upload_content_type,
                        model=model,
                    )
                elapsed = round(time.monotonic() - t0, 1)

                # ── 繁体→简体（语言为 zh 时）─────────────────────────
                lang = payload.get("language", "")
                if lang == "zh" or (isinstance(lang, str) and lang.startswith("zh")):
                    converter = self._get_opencc()
                    if converter and converter is not False:
                        payload = self._simplify_text(payload, converter)

                await self._write_outputs(payload, text_path, json_path)
                await self._record_job(
                    aweme_id=aweme_id,
                    video_path=video_path,
                    transcript_dir=text_path.parent,
                    text_path=text_path,
                    json_path=json_path,
                    model=model,
                    status="success",
                    skip_reason=None,
                    error_message=None,
                )
                return {
                    "status": "success",
                    "text_path": str(text_path),
                    "json_path": str(json_path),
                    "duration": elapsed,
                }
            except Exception as exc:
                error_message = str(exc)
                await self._record_job(
                    aweme_id=aweme_id,
                    video_path=video_path,
                    transcript_dir=text_path.parent,
                    text_path=text_path,
                    json_path=json_path,
                    model=model,
                    status="failed",
                    skip_reason=None,
                    error_message=error_message,
                )
                logger.error(
                    "Transcript failed for aweme %s: %s", aweme_id, error_message
                )
                elapsed = round(time.monotonic() - t0, 1)
                return {
                    "status": "failed",
                    "reason": "transcription_error",
                    "error": error_message,
                    "duration": elapsed,
                }
        finally:
            if tmp_audio_dir is not None:
                try:
                    tmp_audio_dir.cleanup()
                except Exception as exc:
                    logger.warning(
                        "Failed to clean up transcript audio temp dir %s: %r",
                        tmp_audio_dir.name,
                        exc,
                    )

    @staticmethod
    def _to_writable_path(path: Path) -> str:
        """将路径转为可写入的绝对路径字符串。

        Windows 上通过 ``\\\\?\\`` 扩展前缀突破 MAX_PATH (260字符) 限制。
        """
        resolved = str(path.resolve())
        if os.name == "nt" and not resolved.startswith("\\\\?\\"):
            resolved = "\\\\?\\" + resolved
        return resolved

    async def _write_outputs(
        self, payload: Dict[str, Any], text_path: Path, json_path: Path
    ) -> None:
        formats = set(self._response_formats())

        if "txt" in formats:
            text = str(payload.get("text", "")).strip()
            write_path = self._to_writable_path(text_path)
            async with aiofiles.open(write_path, "w", encoding="utf-8") as f:
                await f.write(text)

        if "json" in formats:
            write_path = self._to_writable_path(json_path)
            async with aiofiles.open(write_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(payload, ensure_ascii=False, indent=2))

    async def _call_openai_transcription(
        self,
        *,
        api_key: str,
        file_path: Path,
        filename: str,
        content_type: str,
        model: str,
    ) -> Dict[str, Any]:
        """POST a multipart transcription request.

        ``file_path`` is whatever the caller decided to upload — could be
        the original video, the source audio file (passthrough), or the
        ffmpeg-extracted mp3. The caller passes the appropriate
        ``filename`` + ``content_type`` so the multipart body advertises
        the right MIME.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Upload file not found: {file_path}")

        transcript_cfg = self._cfg()
        language_hint = str(transcript_cfg.get("language_hint", "")).strip()
        api_url = self._api_url()

        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field("response_format", "json")
        if language_hint:
            form.add_field("language", language_hint)

        with file_path.open("rb") as f:
            form.add_field(
                "file",
                f,
                filename=filename,
                content_type=content_type,
            )
            timeout = aiohttp.ClientTimeout(total=600)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    api_url,
                    data=form,
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        # Some misbehaving proxies echo the bearer token
                        # into 4xx error pages; redact before the body
                        # ends up in ``transcript_jobs.error_message``
                        # (Property 1 / 2).
                        if api_key and api_key in body:
                            body = body.replace(api_key, _mask_api_key_local(api_key))
                        raise RuntimeError(
                            f"OpenAI transcription failed: status={response.status}, body={body}"
                        )

                    payload = await response.json(content_type=None)
                    if not isinstance(payload, dict):
                        raise RuntimeError("OpenAI transcription returned invalid payload")
                    return payload

    # ── Local Whisper backend ────────────────────────────────────────

    def _get_whisper_model(self):
        """Lazy-load and cache the faster-whisper model."""
        if hasattr(self, "_whisper") and self._whisper is not None:
            print(f"[whisper] 模型已缓存 (device={self._whisper_device()}, compute={self._whisper_compute_type()})", file=sys.stderr, flush=True)
            logger.warning(
                "Whisper model already loaded (device=%s, compute=%s), reusing cached instance",
                self._whisper_device(), self._whisper_compute_type(),
            )
            return self._whisper

        import ctranslate2
        print(f"[whisper] ctranslate2 {ctranslate2.__version__}, CUDA设备数={ctranslate2.get_cuda_device_count()}", file=sys.stderr, flush=True)

        from faster_whisper import WhisperModel
        print("[whisper] faster_whisper 导入成功，开始加载模型...", file=sys.stderr, flush=True)

        size = self._whisper_model_size()
        device = self._whisper_device()
        compute = self._whisper_compute_type()
        logger.warning(
            "⏳ Loading Whisper model: %s (device=%s, compute=%s) — "
            "首次使用需从 HuggingFace 下载模型文件（~3GB），请耐心等待...",
            size, device, compute,
        )
        sys.stderr.flush()

        # 诊断：检查 ctranslate2 CUDA 支持
        try:
            import ctranslate2
            logger.warning("ctranslate2 版本: %s, CUDA 可用: %s", ctranslate2.__version__, ctranslate2.get_cuda_device_count())
        except Exception:
            logger.warning("无法检测 ctranslate2 CUDA 状态")

        t0 = time.monotonic()
        print(f"[whisper] 正在加载 {size} 模型 (device={device}, compute={compute})...", file=sys.stderr, flush=True)
        self._whisper = WhisperModel(size, device=device, compute_type=compute)
        elapsed = time.monotonic() - t0
        print(f"[whisper] 模型加载完成 {elapsed:.1f}s", file=sys.stderr, flush=True)
        logger.warning(
            "✅ Whisper model loaded in %.1fs: %s (device=%s, compute=%s)",
            elapsed, size, device, compute,
        )
        return self._whisper

    async def _call_whisper_transcription(
        self, *, file_path: Path, model_size: str
    ) -> Dict[str, Any]:
        """Transcribe audio with local faster-whisper, returning an
        OpenAI-compatible payload dict so ``_write_outputs`` works unchanged.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {file_path}")

        file_size_mb = file_path.stat().st_size / 1024 / 1024
        device = self._whisper_device()
        compute = self._whisper_compute_type()
        device_label = "GPU(CUDA)" if device == "cuda" else f"CPU({compute})"
        logger.warning(
            "🎙 转录 %s (%.1fMB, %s, %s)",
            file_path.stem, file_size_mb, model_size, device_label,
        )

        # 本地 Whisper 模型不支持并发调用（CTranslate2 非线程安全），
        # 使用 asyncio.Lock 确保同一时间只有一个转录任务在执行。
        print("[whisper] 获取模型锁...", file=sys.stderr, flush=True)
        async with self._whisper_lock:
            print("[whisper] 调用 _get_whisper_model...", file=sys.stderr, flush=True)
            model = self._get_whisper_model()
            print("[whisper] 模型就绪，开始推理...", file=sys.stderr, flush=True)
            language = self._language()

            _run = partial(
                model.transcribe,
                str(file_path),
                language=language,
                vad_filter=True,
            )
            logger.warning("▶ 开始 faster-whisper 推理（VAD + 识别）...")
            sys.stderr.flush()
            t0 = time.monotonic()
            loop = asyncio.get_running_loop()
            segments_iter, info = await loop.run_in_executor(None, _run)
            logger.warning(
                "▶ 推理完成 (%.1fs)，正在收集 segments...",
                time.monotonic() - t0,
            )
            sys.stderr.flush()

            # 逐个收集 segment 并打印进度
            segments = []
            t_seg_last = time.monotonic()
            for i, seg in enumerate(segments_iter):
                segments.append(seg)
                now = time.monotonic()
                if now - t_seg_last >= 5.0:
                    logger.warning(
                        "  转录进度: %d segments 已识别 (%.1fs)...",
                        len(segments), now - t0,
                    )
                    sys.stderr.flush()
                    t_seg_last = now

        elapsed_total = time.monotonic() - t0
        detected_lang = info.language if info and info.language else (language or "zh")

        logger.warning(
            "✅ Whisper 转录完成: %d segments, 语言=%s, 总耗时 %.1fs",
            len(segments),
            detected_lang,
            elapsed_total,
        )

        text = "".join(
            (seg.text or "").strip() + ("\n" if i < len(segments) - 1 else "")
            for i, seg in enumerate(segments)
        ).strip()

        # Build OpenAI-compatible payload so _write_outputs + _record_job
        # don't need to know which backend ran.
        return {
            "text": text,
            "model": f"whisper-{model_size}",
            "language": detected_lang,
            "segments": [
                {
                    "id": i,
                    "start": round(seg.start, 3),
                    "end": round(seg.end, 3),
                    "text": (seg.text or "").strip(),
                }
                for i, seg in enumerate(segments)
            ],
        }

    @staticmethod
    def _guess_video_content_type(video_path: Path) -> str:
        suffix = video_path.suffix.lower()
        if suffix == ".mp4":
            return "video/mp4"
        if suffix == ".m4a":
            return "audio/mp4"
        if suffix == ".wav":
            return "audio/wav"
        if suffix == ".mp3":
            return "audio/mpeg"
        return "application/octet-stream"

    async def _record_job(
        self,
        *,
        aweme_id: str,
        video_path: Path,
        transcript_dir: Path,
        text_path: Path,
        json_path: Path,
        model: str,
        status: str,
        skip_reason: Optional[str],
        error_message: Optional[str],
    ) -> None:
        if not self.database:
            return

        await self.database.upsert_transcript_job(
            {
                "aweme_id": aweme_id,
                "video_path": str(video_path),
                "transcript_dir": str(transcript_dir),
                "text_path": str(text_path),
                "json_path": str(json_path),
                "model": model,
                "status": status,
                "skip_reason": skip_reason,
                "error_message": error_message,
            }
        )
