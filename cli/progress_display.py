from __future__ import annotations

import sys
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

# Windows 下使用 UTF-8 编码，避免 Unicode 编码错误
if sys.platform == "win32":
    console = Console(force_terminal=True, legacy_windows=False)
else:
    console = Console()


class ProgressDisplay:
    _URL_STEP_TOTAL = 6

    def __init__(self):
        self.console = console
        self._progress_ctx: Optional[Progress] = None
        self._progress: Optional[Progress] = None
        self._overall_task_id: Optional[int] = None
        self._url_task_id: Optional[int] = None
        self._url_index = 0
        self._url_total = 0
        self._url_step_completed = 0
        self._item_total = 0
        self._item_completed = 0
        self._display_item_total: Optional[int] = None  # 用于显示"共 N 个作品"，与进度 total 解耦
        self._single_url_item_mode = False
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0}

    def show_banner(self):
        banner = """
╔══════════════════════════════════════════╗
║     Douyin Downloader v2.0.0            ║
║     抖音批量下载工具                     ║
╚══════════════════════════════════════════╝
        """
        self._active_console().print(banner, style="bold cyan")

    def create_progress(self) -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[detail]}"),
            console=self.console,
            transient=True,
            refresh_per_second=6,
        )

    def start_download_session(self, total_urls: int):
        if self._progress is not None:
            return

        self._progress_ctx = self.create_progress()
        self._progress = self._progress_ctx.__enter__()
        self._single_url_item_mode = False
        self._overall_task_id = self._progress.add_task(
            "总体进度",
            total=max(total_urls, 1),
            completed=0,
            detail=f"共 {total_urls} 个 URL",
        )

    def stop_download_session(self):
        self._cleanup_url_tasks()

        if self._progress_ctx is not None:
            self._progress_ctx.__exit__(None, None, None)

        self._progress_ctx = None
        self._progress = None
        self._overall_task_id = None
        self._single_url_item_mode = False

    def start_url(self, index: int, total: int, url: str):
        self._url_index = index
        self._url_total = total
        self._url_step_completed = 0
        self._item_total = 0
        self._item_completed = 0
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0}

        self._cleanup_url_tasks()
        if not self._progress:
            return

        self._url_task_id = self._progress.add_task(
            self._format_url_description("待开始"),
            total=self._URL_STEP_TOTAL,
            completed=0,
            detail=self._shorten(url, max_len=72),
        )

    def complete_url(self, result=None):
        if self._progress and self._url_task_id is not None:
            detail = ""
            if result:
                detail = f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}"
            self._progress.update(
                self._url_task_id,
                completed=self._URL_STEP_TOTAL,
                description=self._format_url_description("完成"),
                detail=detail,
            )

        if self._progress and self._overall_task_id is not None:
            if self._single_url_item_mode:
                self._progress.update(self._overall_task_id, completed=self._item_total or 1)
            else:
                self._progress.advance(self._overall_task_id, 1)

    def fail_url(self, reason: str):
        if self._progress and self._url_task_id is not None:
            self._progress.update(
                self._url_task_id,
                completed=self._URL_STEP_TOTAL,
                description=self._format_url_description("失败"),
                detail=reason,
            )

        if self._progress and self._overall_task_id is not None:
            if self._single_url_item_mode:
                self._progress.update(self._overall_task_id, completed=self._item_total or 1)
            else:
                self._progress.advance(self._overall_task_id, 1)

    def advance_step(self, step: str, detail: str = ""):
        if not self._progress or self._url_task_id is None:
            return

        self._url_step_completed = min(self._url_step_completed + 1, self._URL_STEP_TOTAL)
        self._progress.update(
            self._url_task_id,
            completed=self._url_step_completed,
            description=self._format_url_description(step),
            detail=detail,
        )

    def update_step(self, step: str, detail: str = ""):
        if not self._progress or self._url_task_id is None:
            return

        self._progress.update(
            self._url_task_id,
            description=self._format_url_description(step),
            detail=detail,
        )

    def set_item_total(self, total: int, detail: str = "", display_total: Optional[int] = None):
        if not self._progress:
            return

        self._item_total = max(total, 1)
        self._item_completed = 1 if total == 0 else 0
        self._display_item_total = display_total  # None 表示与 total 相同
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0}

        if self._url_total == 1 and self._overall_task_id is not None:
            self._single_url_item_mode = True
            self._progress.update(
                self._overall_task_id,
                total=self._item_total,
                completed=self._item_completed,
                detail=self._overall_item_detail(),
            )

    def _overall_item_detail(self) -> str:
        """返回总体进度条中显示数量的文本，与进度 total 解耦。"""
        if self._display_item_total is not None:
            n = self._display_item_total
            return f"共 {n} 个视频 + {n} 个文稿"
        return f"共 {self._item_total} 个作品"

    def advance_item(self, status: str, detail: str = ""):
        if not self._progress:
            return

        if status in self._item_stats:
            self._item_stats[status] += 1
        if self._item_completed < self._item_total:
            self._item_completed += 1

        if self._single_url_item_mode and self._overall_task_id is not None:
            self._progress.update(
                self._overall_task_id,
                completed=self._item_completed,
                detail=self._overall_item_detail(),
            )

    def extend_item_total(self, additional: int, detail: str = ""):
        """Increase the item total without resetting completed count.

        Used when transcription work extends the original download-only total,
        so the progress bar reflects download + transcription as one continuum.
        """
        if not self._progress or additional <= 0:
            return
        self._item_total += additional
        if self._single_url_item_mode and self._overall_task_id is not None:
            self._progress.update(
                self._overall_task_id,
                total=self._item_total,
                completed=self._item_completed,
                detail=self._overall_item_detail(),
            )

    def advance_progress(self, detail: str = ""):
        """Advance the overall progress bar without affecting S/F/K stats.

        Used for transcription completions so they move the progress bar
        but don't pollute the download counters.
        """
        if not self._progress:
            return
        if self._item_completed < self._item_total:
            self._item_completed += 1
        if self._single_url_item_mode and self._overall_task_id is not None:
            self._progress.update(
                self._overall_task_id,
                completed=self._item_completed,
                detail=self._overall_item_detail(),
            )

    _TRANSCRIPT_REASON_LABELS = {
        "already_exists": "已存在",
        "disabled": "已禁用",
        "missing_api_key": "缺少 API Key",
        "audio_extract_failed": "音频提取失败",
        "transcription_error": "转录错误",
        "whisper_error": "Whisper 错误",
        "timeout": "超时",
    }

    def show_result(self, result):
        table = Table(title="Download Summary", show_header=True, header_style="bold magenta")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="green")

        table.add_row("Total", str(result.total))
        table.add_row("Success", str(result.success))
        table.add_row("Failed", str(result.failed))
        table.add_row("Skipped", str(result.skipped))

        if result.total > 0:
            success_rate = (result.success / result.total) * 100
            table.add_row("Success Rate", f"{success_rate:.1f}%")

        # ── 转录统计 ──────────────────────────────────────────────
        t_total = result.transcript_success + result.transcript_failed + result.transcript_skipped
        if t_total > 0:
            table.add_section()
            table.add_row("[bold]─ Transcript ─[/bold]", "")
            table.add_row("  Success", str(result.transcript_success))
            table.add_row("  Failed", str(result.transcript_failed))
            for reason, count in sorted(result.transcript_fail_reasons.items()):
                label = self._TRANSCRIPT_REASON_LABELS.get(reason, reason)
                table.add_row(f"    ├ {label}", str(count))
            table.add_row("  Skipped", str(result.transcript_skipped))
            for reason, count in sorted(result.transcript_skip_reasons.items()):
                label = self._TRANSCRIPT_REASON_LABELS.get(reason, reason)
                table.add_row(f"    ├ {label}", str(count))

        self._active_console().print(table)

        # ── 异常明细 ──────────────────────────────────────────────
        if result.issues:
            console = self._active_console()
            console.print()
            console.print("[bold yellow]⚠ Issues:[/bold yellow]")
            for issue in result.issues:
                self._print_issue(console, issue)

    def _print_issue(self, console, issue: dict) -> None:
        desc = issue.get("desc", issue.get("aweme_id", "?"))
        download = issue.get("download")
        transcript = issue.get("transcript")
        transcript_reason = issue.get("transcript_reason", "")
        transcript_error = issue.get("transcript_error", "")
        dl_dur = issue.get("download_duration")
        t_dur = issue.get("transcript_duration")

        # 下载状态标签
        dl_tag = ""
        if download == "failed":
            dur_str = f" ({dl_dur:.1f}s)" if dl_dur is not None else ""
            dl_tag = f" [red]下载失败{dur_str}[/red]"
        elif download == "skipped":
            dl_tag = " [dim]下载跳过[/dim]"

        # 转录状态标签
        t_tag = ""
        if transcript:
            label = self._TRANSCRIPT_REASON_LABELS.get(transcript_reason, transcript_reason)
            dur_str = f" ({t_dur:.1f}s)" if t_dur is not None else ""
            if transcript == "failed":
                error_suffix = f": {transcript_error}" if transcript_error else ""
                t_tag = f" [red]转录失败: {label}{dur_str}{error_suffix}[/red]"
            elif transcript == "skipped":
                t_tag = f" [dim]转录跳过: {label}[/dim]"

        console.print(f"  {desc}{dl_tag}{t_tag}")

    def print_info(self, message: str):
        self._active_console().print(f"[blue][INFO][/blue] {message}")

    def print_success(self, message: str):
        self._active_console().print(f"[green][OK][/green] {message}")

    def print_warning(self, message: str):
        self._active_console().print(f"[yellow][WARN][/yellow] {message}")

    def print_error(self, message: str):
        self._active_console().print(f"[red][ERROR][/red] {message}")

    def _cleanup_url_tasks(self):
        if not self._progress:
            self._url_task_id = None
            return
        if self._url_task_id is not None:
            self._progress.remove_task(self._url_task_id)
            self._url_task_id = None

    def _format_url_description(self, step: str) -> str:
        return f"URL {self._url_index}/{self._url_total} · {step}"

    def _active_console(self) -> Console:
        if self._progress:
            return self._progress.console
        return self.console

    @staticmethod
    def _shorten(text: str, max_len: int = 60) -> str:
        normalized = (text or "").strip()
        if len(normalized) <= max_len:
            return normalized
        return f"{normalized[: max_len - 3]}..."
