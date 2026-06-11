"""
Custom Textual widgets used by the chat log.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import RenderResult, Console
from rich.padding import Padding
from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..agent import _format_diff


# True black & white pygments style — keeps code blocks grayscale.
try:
    from pygments.styles.bw import BlackWhiteStyle
    _CODE_THEME = BlackWhiteStyle
except Exception:
    _CODE_THEME = "monokai"


# ── CodeBlock: shows diff with proper colors ────────────────────────────────
class CodeBlock(Static):
    """A syntax-highlighted code block. Used for tool args, file contents, diffs."""

    DEFAULT_CSS = """
    CodeBlock {
        background: #111111;
        border: round #2e2e2e;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
        max-height: 20;
        overflow-y: auto;
    }
    """

    def __init__(self, content: str, language: str = "text", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._content = content
        self._language = language

    def render(self) -> RenderResult:
        if not self._content:
            return Text("")
        try:
            return Syntax(
                self._content,
                self._language,
                theme=_CODE_THEME,
                word_wrap=False,
                background_color="#111111",
            )
        except Exception:
            return Text(self._content)


# ── ToolCard: live-updating card for an in-flight tool call ────────────────
class ToolCard(Container):
    """Visual card for a single tool invocation."""

    DEFAULT_CSS = """
    ToolCard {
        background: #1a1a1a;
        border: round #2e2e2e;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    ToolCard.running {
        border: round #ffffff;
    }
    ToolCard.done {
        border: round #2e2e2e;
    }
    ToolCard.error {
        border: round #ffffff;
    }
    .tool-header {
        height: 1;
        color: #ffffff;
        text-style: bold;
    }
    .tool-header.done {
        color: #c0c0c0;
    }
    .tool-header.error {
        color: #ffffff;
    }
    .tool-name {
        color: #ffffff;
        text-style: bold;
    }
    .tool-state {
        color: #a8a8a8;
    }
    .tool-args {
        color: #a8a8a8;
        margin: 0 0 1 0;
    }
    .tool-result {
        color: #a8a8a8;
        margin: 0;
    }
    .tool-args-label, .tool-result-label {
        color: #7a7a7a;
        text-style: italic;
    }
    """

    state: reactive[str] = reactive("running")

    def __init__(self, tool_name: str, args: Dict[str, Any]) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = args
        self._result: Optional[Dict[str, Any]] = None
        self._elapsed: float = 0.0
        self._diff_widget: Optional[CodeBlock] = None

    def compose(self) -> ComposeResult:
        yield Static(self._render_header(), classes="tool-header")
        args_text = self._shorten(json.dumps(self.args, ensure_ascii=False, default=str), 240)
        yield Static(f"[dim]args:[/dim] {args_text}", classes="tool-args", markup=True)

    def _render_header(self) -> str:
        if self.state == "running":
            return f"● [bold]running[/bold]  [#ffffff]{self.tool_name}[/]  [#7a7a7a]{self._elapsed:0.1f}s[/]"
        if self.state == "error":
            err = (self._result or {}).get("error", "failed")
            return f"✗ [bold]error[/bold]    [#ffffff]{self.tool_name}[/]  [#7a7a7a]{self._elapsed:0.2f}s[/]  [#ffffff]{err[:80]}[/]"
        status = (self._result or {}).get("action") or "ok"
        return f"✓ [bold]done[/bold]     [#ffffff]{self.tool_name}[/]  [#7a7a7a]{self._elapsed:0.2f}s[/]  [dim]→ {status}[/]"

    def _shorten(self, text: str, max_len: int) -> str:
        text = text.replace("\n", " ")
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def update_result(self, result: Dict[str, Any], elapsed: float) -> None:
        self._result = result
        self._elapsed = elapsed
        if "error" in result:
            self.state = "error"
        else:
            self.state = "done"
        self._refresh_view()

    def _refresh_view(self) -> None:
        self.set_class(self.state == "running", "running")
        self.set_class(self.state == "done", "done")
        self.set_class(self.state == "error", "error")
        try:
            header = self.query_one(".tool-header", Static)
            header.update(self._render_header())
            header.set_class(self.state == "done", "done")
            header.set_class(self.state == "error", "error")
        except Exception:
            pass
        try:
            result_widget = self.query(".tool-result")
            if result_widget:
                result_widget[0].remove()
        except Exception:
            pass

        if self._result is not None:
            try:
                preview = self._result_preview(self._result)
                self.mount(Static(
                    f"[dim]result:[/dim] {preview}",
                    classes="tool-result",
                    markup=True,
                ))
            except Exception:
                pass

    def _result_preview(self, result: Dict[str, Any]) -> str:
        if "error" in result:
            return f"[#ffffff]{self._shorten(str(result['error']), 200)}[/]"
        if "content" in result and isinstance(result["content"], str):
            return f"[dim]read {len(result['content'])} chars[/]"
        if "files" in result:
            return f"[dim]read {len(result['files'])} files[/]"
        if "matches" in result and isinstance(result["matches"], list):
            return f"[dim]{len(result['matches'])} matches[/]"
        action = result.get("action") or "ok"
        return f"[#e6e6e6]{action}[/]"

    def attach_diff(self, diff_widget: CodeBlock) -> None:
        if self._diff_widget is not None:
            return
        self._diff_widget = diff_widget
        self.mount(diff_widget)


# ── TerminalCard: dedicated visual for sandboxed shell commands ─────────────
class TerminalCard(Container):
    """Visual card for a single `terminal` tool invocation.

    Renders the command in a shell-prompt style box with stdout/stderr panels
    and a status row (exit code, elapsed time, cwd). Visually distinct from
    a regular ToolCard so the user can tell at a glance that a shell command
    ran.
    """

    DEFAULT_CSS = """
    TerminalCard {
        background: #0d0d0d;
        border: round #ffffff;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    TerminalCard.running {
        border: round #ffffff;
    }
    TerminalCard.done {
        border: round #2e2e2e;
    }
    TerminalCard.error {
        border: round #ffffff;
    }
    .term-prompt {
        color: #dcdcdc;
        text-style: bold;
        height: 1;
    }
    .term-cmd {
        color: #dcdcdc;
        background: #111111;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
    }
    .term-status {
        color: #b8b8b8;
        margin: 0 0 1 0;
        height: 1;
    }
    .term-status.error {
        color: #f0f0f0;
        text-style: bold;
    }
    .term-status.ok {
        color: #c8c8c8;
    }
    .term-section-label {
        color: #8a8a8a;
        text-style: italic;
        height: 1;
    }
    .term-output {
        color: #dcdcdc;
        background: #111111;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
        max-height: 18;
        overflow-y: auto;
    }
    .term-output-empty {
        color: #6a6a6a;
        text-style: italic;
    }
    """

    state: reactive[str] = reactive("running")

    MAX_INLINE_CHARS = 4000

    def __init__(self, args: Dict[str, Any]) -> None:
        super().__init__()
        self._command: str = str(args.get("command", "") or "")
        self._cwd_arg: str = str(args.get("cwd", "") or "")
        self._result: Optional[Dict[str, Any]] = None
        self._elapsed: float = 0.0

    def compose(self) -> ComposeResult:
        yield Static(self._render_prompt(), classes="term-prompt", markup=True)
        yield Static(self._render_command(), classes="term-cmd", markup=True)
        yield Static("", classes="term-status")
        yield Static("", classes="term-section-label")
        yield Static("", classes="term-output")

    def _render_prompt(self) -> str:
        if self.state == "running":
            return "▸ terminal · running"
        if self.state == "error":
            return "▸ terminal · exit error"
        return "▸ terminal · done"

    def _render_command(self) -> str:
        if not self._command:
            return "[dim](empty command)[/dim]"
        escaped = self._command.replace("[", "\\[")
        return f"$ [bold #dcdcdc]{escaped}[/]"

    def _truncate(self, text: str) -> str:
        if not text:
            return ""
        if len(text) <= self.MAX_INLINE_CHARS:
            return text
        return (
            text[: self.MAX_INLINE_CHARS]
            + f"\n... [truncated for display, {len(text)} chars total]"
        )

    def update_result(self, result: Dict[str, Any], elapsed: float) -> None:
        self._result = result
        self._elapsed = elapsed
        if "error" in result:
            self.state = "error"
        else:
            self.state = "done"
        self._refresh_view()

    def _refresh_view(self) -> None:
        self.set_class(self.state == "running", "running")
        self.set_class(self.state == "done", "done")
        self.set_class(self.state == "error", "error")
        try:
            prompt = self.query_one(".term-prompt", Static)
            prompt.update(self._render_prompt())
        except Exception:
            pass
        try:
            status = self.query_one(".term-status", Static)
            label = self.query_one(".term-section-label", Static)
            output = self.query_one(".term-output", Static)

            status.set_class(False, "error")
            status.set_class(False, "ok")

            cwd_text = (
                f"[dim]cwd:[/dim] {self._cwd_arg}" if self._cwd_arg else "[dim]cwd:[/dim] (sandbox root)"
            )

            if self._result is None:
                status.update("")
                label.update("")
                output.update("")
                return

            exit_code = self._result.get("exit_code", "—")
            stdout_chars = self._result.get("stdout_chars", 0)
            stderr_chars = self._result.get("stderr_chars", 0)
            truncated = bool(self._result.get("truncated"))

            status_line = (
                f"{cwd_text}  [dim]·[/dim]  exit [bold #dcdcdc]{exit_code}[/]  "
                f"[dim]·[/dim]  {self._elapsed:0.2f}s  "
                f"[dim]·[/dim]  stdout {stdout_chars}c  stderr {stderr_chars}c"
                + ("  [dim]·[/dim]  [italic]output offloaded[/italic]" if truncated else "")
            )

            if self.state == "error":
                err_text = str(self._result.get("error", ""))
                status.set_class(True, "error")
                status.update(status_line + (f"  ·  {err_text}" if err_text else ""))
            else:
                status.set_class(True, "ok")
                status.update(status_line)

            stdout = str(self._result.get("stdout", "") or "")
            stderr = str(self._result.get("stderr", "") or "")

            if not stdout and not stderr:
                label.update("[dim]output[/dim]")
                output.set_class(True, "term-output-empty")
                output.update("(no output)")
                return

            sections: list = []
            if stdout:
                sections.append(f"[dim]stdout[/dim]\n{self._truncate(stdout)}")
            if stderr:
                sections.append(f"[dim]stderr[/dim]\n{self._truncate(stderr)}")
            label.update("[dim]output[/dim]")
            output.set_class(False, "term-output-empty")
            output.update("\n\n".join(sections))
        except Exception:
            pass



# ── User message ────────────────────────────────────────────────────────────
class UserMessage(Container):
    DEFAULT_CSS = """
    UserMessage {
        background: #1a1a1a;
        color: #e6e6e6;
        padding: 1 2;
        margin: 1 0 0 6;
        border: round #2e2e2e;
        height: auto;
    }
    .user-prefix {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    .user-body {
        color: #e6e6e6;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static("● You", classes="user-prefix")
        yield Static(self._text, classes="user-body", markup=False)


# ── Assistant message ───────────────────────────────────────────────────────
class AssistantMessage(Container):
    DEFAULT_CSS = """
    AssistantMessage {
        background: transparent;
        color: #e6e6e6;
        padding: 1 2;
        margin: 1 6 0 0;
        height: auto;
    }
    .assistant-prefix {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    .assistant-body {
        color: #e6e6e6;
    }
    .assistant-body-streaming {
        color: #c0c0c0;
    }
    """

    def __init__(self, model: str) -> None:
        super().__init__()
        self._model = model
        self._text_parts: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(f"◆ IndusBuilt · [dim]{self._model}[/]", classes="assistant-prefix", markup=True)
        yield Static("", classes="assistant-body")

    def append_token(self, token: str) -> None:
        self._text_parts.append(token)
        try:
            body = self.query_one(".assistant-body", Static)
            body.update("".join(self._text_parts))
        except Exception:
            pass

    def finalize(self) -> None:
        try:
            body = self.query_one(".assistant-body", Static)
            body.set_class(False, "assistant-body-streaming")
        except Exception:
            pass


# ── System messages ─────────────────────────────────────────────────────────
class SystemInfo(Static):
    DEFAULT_CSS = """
    SystemInfo {
        color: #a8a8a8;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemSuccess(Static):
    DEFAULT_CSS = """
    SystemSuccess {
        color: #ffffff;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemWarning(Static):
    DEFAULT_CSS = """
    SystemWarning {
        color: #c0c0c0;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemError(Static):
    DEFAULT_CSS = """
    SystemError {
        color: #ffffff;
        text-style: bold;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemMemory(Container):
    DEFAULT_CSS = """
    SystemMemory {
        background: #1a1a1a;
        border: round #2e2e2e;
        color: #e6e6e6;
        padding: 1 2;
        margin: 1 0;
        height: auto;
    }
    .memory-title {
        color: #ffffff;
        text-style: bold;
        margin-bottom: 1;
    }
    .memory-row {
        color: #c0c0c0;
    }
    .memory-label {
        color: #9a9a9a;
    }
    .memory-value {
        color: #e6e6e6;
    }
    """

    def __init__(self, status: Dict[str, Any]) -> None:
        super().__init__()
        self._status = status

    def compose(self) -> ComposeResult:
        yield Static("Memory status", classes="memory-title")
        yield Static(self._format_row("Memory root", str(self._status.get("memory_root", "—"))), classes="memory-row", markup=True)
        yield Static(self._format_row("Indexed entries", str(self._status.get("indexed_entries", 0))), classes="memory-row", markup=True)
        yield Static(self._format_row("Knowledge files", str(self._status.get("knowledge_files", 0))), classes="memory-row", markup=True)
        yield Static(self._format_row("Summary chars", str(self._status.get("summary_chars", 0))), classes="memory-row", markup=True)
        active = self._status.get("active_context") or {}
        for key in ("goal", "current_file", "active_bug", "next_step"):
            value = str(active.get(key) or "—")
            yield Static(self._format_row(key.replace("_", " ").title(), value), classes="memory-row", markup=True)

    def _format_row(self, label: str, value: str) -> str:
        return f"[dim]# {label}[/dim]\n  [italic]{value}[/italic]"


# ── Hook notice ────────────────────────────────────────────────────────────
class HookNotice(Static):
    DECISION_CLASS = {
        "allow": "hook-decision-allow",
        "block": "hook-decision-block",
        "warn":  "hook-decision-warn",
        "ask":   "hook-decision-ask",
    }

    DEFAULT_CSS = """
    HookNotice {
        color: #a8a8a8;
        padding: 0 2;
        margin: 0;
        height: 1;
    }
    .hook-decision-allow { color: #e6e6e6; }
    .hook-decision-block { color: #ffffff; text-style: bold; }
    .hook-decision-warn  { color: #c0c0c0; }
    .hook-decision-ask   { color: #ffffff; }
    """

    def __init__(self, hook_name: str, event: str, decision: str, reason: str, elapsed: float, error: Optional[str]) -> None:
        super().__init__()
        self._hook_name = hook_name
        self._event = event
        self._decision = decision
        self._reason = reason
        self._elapsed = elapsed
        self._error = error

    def render(self) -> RenderResult:
        text = Text()
        text.append("⚙ ", style="#ffffff")
        text.append("hook ", style="dim")
        text.append(self._hook_name, style="bold #ffffff")
        text.append(f"  {self._event}  ", style="dim")
        text.append(self._decision, style=f"bold {self._decision_color()}")
        text.append(f"  {self._elapsed:0.2f}s", style="dim")
        if self._reason:
            text.append(f"  · {self._reason[:80]}", style="italic #9a9a9a")
        if self._error:
            text.append(f"  · {self._error[:80]}", style="#ffffff bold")
        return text

    def _decision_color(self) -> str:
        return {
            "allow": "#e6e6e6",
            "block": "#ffffff",
            "warn":  "#c0c0c0",
            "ask":   "#ffffff",
        }.get(self._decision, "#e6e6e6")


# ── Subagent card ──────────────────────────────────────────────────────────
class SubagentCard(Container):
    DEFAULT_CSS = """
    SubagentCard {
        background: #1a1a1a;
        border: round #ffffff;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    .subagent-header {
        color: #ffffff;
        text-style: bold;
        height: 1;
    }
    .subagent-status-running {
        color: #ffffff;
    }
    .subagent-status-done {
        color: #c0c0c0;
    }
    .subagent-status-error {
        color: #ffffff;
    }
    .subagent-task {
        color: #a8a8a8;
        margin: 0 0 1 0;
    }
    .subagent-output {
        color: #e6e6e6;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, name: str, task: str) -> None:
        super().__init__()
        self._name = name
        self._task = task
        self._state = "running"

    def compose(self) -> ComposeResult:
        yield Static(self._header(), classes="subagent-header", markup=True)
        if self._task:
            yield Static(f"[dim]task:[/dim] {self._shorten(self._task, 200)}", classes="subagent-task", markup=True)
        yield Static("", classes="subagent-output")

    def _header(self) -> str:
        if self._state == "running":
            return f"● running subagent  [#ffffff]{self._name}[/]"
        if self._state == "error":
            return f"✗ subagent error   [#ffffff]{self._name}[/]"
        return f"✓ subagent done    [#ffffff]{self._name}[/]"

    def _shorten(self, text: str, max_len: int) -> str:
        text = text.replace("\n", " ")
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def update_result(self, output: str, elapsed: float, turns: int, error: Optional[str]) -> None:
        self._state = "error" if error else "done"
        try:
            header = self.query_one(".subagent-header", Static)
            header.update(self._header() + f"  [dim]{elapsed:0.1f}s · {turns} turns[/]")
        except Exception:
            pass
        try:
            out = self.query_one(".subagent-output", Static)
            if error:
                out.update(f"[#ffffff]{error}[/]")
            else:
                preview = self._shorten(output, 600)
                out.update(preview)
        except Exception:
            pass


# ── Thinking indicator ─────────────────────────────────────────────────────
class ThinkingIndicator(Static):
    DEFAULT_CSS = """
    ThinkingIndicator {
        background: transparent;
        color: #ffffff;
        padding: 0 2;
        height: 1;
    }
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str = "thinking") -> None:
        super().__init__()
        self._label = label
        self._frame_index = 0
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(self._FRAMES)
        frame = self._FRAMES[self._frame_index]
        self.update(f"{frame} [#ffffff]{self._label}[/]")

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
