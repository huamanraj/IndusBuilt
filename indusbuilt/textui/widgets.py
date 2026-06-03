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


# ── CodeBlock: shows diff with proper colors ────────────────────────────────
class CodeBlock(Static):
    """A syntax-highlighted code block. Used for tool args, file contents, diffs."""

    DEFAULT_CSS = """
    CodeBlock {
        background: #181825;
        border: round #45475a;
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
            return Syntax(self._content, self._language, theme="monokai", word_wrap=False)
        except Exception:
            return Text(self._content)


# ── ToolCard: live-updating card for an in-flight tool call ────────────────
class ToolCard(Container):
    """Visual card for a single tool invocation."""

    DEFAULT_CSS = """
    ToolCard {
        background: #313244;
        border: round #45475a;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    ToolCard.running {
        border: round #89b4fa;
    }
    ToolCard.done {
        border: round #a6e3a1;
    }
    ToolCard.error {
        border: round #f38ba8;
    }
    .tool-header {
        height: 1;
        color: #89b4fa;
        text-style: bold;
    }
    .tool-header.done {
        color: #a6e3a1;
    }
    .tool-header.error {
        color: #f38ba8;
    }
    .tool-name {
        color: #cba6f7;
        text-style: bold;
    }
    .tool-state {
        color: #a6adc8;
    }
    .tool-args {
        color: #a6adc8;
        margin: 0 0 1 0;
    }
    .tool-result {
        color: #a6adc8;
        margin: 0;
    }
    .tool-args-label, .tool-result-label {
        color: #9399b2;
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
            return f"● [bold]running[/bold]  [#cba6f7]{self.tool_name}[/]  [#9399b2]{self._elapsed:0.1f}s[/]"
        if self.state == "error":
            err = (self._result or {}).get("error", "failed")
            return f"✗ [bold]error[/bold]    [#cba6f7]{self.tool_name}[/]  [#9399b2]{self._elapsed:0.2f}s[/]  [#f38ba8]{err[:80]}[/]"
        status = (self._result or {}).get("action") or "ok"
        return f"✓ [bold]done[/bold]     [#cba6f7]{self.tool_name}[/]  [#9399b2]{self._elapsed:0.2f}s[/]  [dim]→ {status}[/]"

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
            return f"[#f38ba8]{self._shorten(str(result['error']), 200)}[/]"
        if "content" in result and isinstance(result["content"], str):
            return f"[dim]read {len(result['content'])} chars[/]"
        if "files" in result:
            return f"[dim]read {len(result['files'])} files[/]"
        if "matches" in result and isinstance(result["matches"], list):
            return f"[dim]{len(result['matches'])} matches[/]"
        if "tree" in result:
            tree = result.get("tree", "")
            return f"[dim]{len(tree.splitlines())} tree lines[/]"
        action = result.get("action") or "ok"
        return f"[#a6e3a1]{action}[/]"

    def attach_diff(self, diff_widget: CodeBlock) -> None:
        if self._diff_widget is not None:
            return
        self._diff_widget = diff_widget
        self.mount(diff_widget)


# ── User message ────────────────────────────────────────────────────────────
class UserMessage(Container):
    DEFAULT_CSS = """
    UserMessage {
        background: #313244;
        color: #cdd6f4;
        padding: 1 2;
        margin: 1 0 0 6;
        border: round #45475a;
        height: auto;
    }
    .user-prefix {
        color: #fab387;
        text-style: bold;
        margin-bottom: 1;
    }
    .user-body {
        color: #cdd6f4;
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
        color: #cdd6f4;
        padding: 1 2;
        margin: 1 6 0 0;
        height: auto;
    }
    .assistant-prefix {
        color: #89b4fa;
        text-style: bold;
        margin-bottom: 1;
    }
    .assistant-body {
        color: #cdd6f4;
    }
    .assistant-body-streaming {
        color: #bac2de;
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
        color: #a6adc8;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemSuccess(Static):
    DEFAULT_CSS = """
    SystemSuccess {
        color: #a6e3a1;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemWarning(Static):
    DEFAULT_CSS = """
    SystemWarning {
        color: #f9e2af;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemError(Static):
    DEFAULT_CSS = """
    SystemError {
        color: #f38ba8;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    """


class SystemMemory(Container):
    DEFAULT_CSS = """
    SystemMemory {
        background: #313244;
        border: round #45475a;
        color: #cdd6f4;
        padding: 1 2;
        margin: 1 0;
        height: auto;
    }
    .memory-title {
        color: #cba6f7;
        text-style: bold;
        margin-bottom: 1;
    }
    .memory-row {
        color: #bac2de;
    }
    .memory-label {
        color: #9399b2;
    }
    .memory-value {
        color: #cdd6f4;
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
        color: #a6adc8;
        padding: 0 2;
        margin: 0;
        height: 1;
    }
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
        decision_class = self.DECISION_CLASS.get(self._decision, "hook-decision-allow")
        text = Text()
        text.append("⚙ ", style="#cba6f7")
        text.append("hook ", style="dim")
        text.append(self._hook_name, style="bold #cba6f7")
        text.append(f"  {self._event}  ", style="dim")
        text.append(self._decision, style=f"bold {self._decision_color()}")
        text.append(f"  {self._elapsed:0.2f}s", style="dim")
        if self._reason:
            text.append(f"  · {self._reason[:80]}", style="italic #9399b2")
        if self._error:
            text.append(f"  · {self._error[:80]}", style="#f38ba8")
        return text

    def _decision_color(self) -> str:
        return {
            "allow": "#a6e3a1",
            "block": "#f38ba8",
            "warn":  "#f9e2af",
            "ask":   "#cba6f7",
        }.get(self._decision, "#a6e3a1")


# ── Subagent card ──────────────────────────────────────────────────────────
class SubagentCard(Container):
    DEFAULT_CSS = """
    SubagentCard {
        background: #313244;
        border: round #cba6f7;
        padding: 0 2;
        margin: 1 0;
        height: auto;
    }
    .subagent-header {
        color: #cba6f7;
        text-style: bold;
        height: 1;
    }
    .subagent-status-running {
        color: #89b4fa;
    }
    .subagent-status-done {
        color: #a6e3a1;
    }
    .subagent-status-error {
        color: #f38ba8;
    }
    .subagent-task {
        color: #a6adc8;
        margin: 0 0 1 0;
    }
    .subagent-output {
        color: #cdd6f4;
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
            return f"● running subagent  [#cba6f7]{self._name}[/]"
        if self._state == "error":
            return f"✗ subagent error   [#cba6f7]{self._name}[/]"
        return f"✓ subagent done    [#cba6f7]{self._name}[/]"

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
                out.update(f"[#f38ba8]{error}[/]")
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
        color: #89b4fa;
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
        self.update(f"{frame} [#89b4fa]{self._label}[/]")

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
