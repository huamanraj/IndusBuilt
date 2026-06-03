"""
Chat screen — main interactive view.

Static input docked at the bottom, scrollable history filling the middle,
compact header pinned to the top with model + hint. An inline slash-command
autocomplete dropdown floats above the input when the user types `/`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from ..agent import SLASH_COMMANDS
from .theme import TUI_CSS
from .widgets import (
    AssistantMessage,
    CodeBlock,
    HookNotice,
    SubagentCard,
    SystemError,
    SystemInfo,
    SystemMemory,
    SystemSuccess,
    SystemWarning,
    ThinkingIndicator,
    ToolCard,
    UserMessage,
)

if TYPE_CHECKING:
    from .app import IndusBuiltApp


class SlashSuggestions(Container):
    """Autocomplete dropdown for slash commands."""

    DEFAULT_CSS = """
    SlashSuggestions {
        background: #313244;
        border: round #45475a;
        height: auto;
        max-height: 12;
        margin: 0 2;
        padding: 0 1;
    }
    SlashSuggestions.hidden {
        display: none;
    }
    .suggestion-item {
        height: 1;
        padding: 0 1;
        color: #cdd6f4;
    }
    .suggestion-item.selected {
        background: #89b4fa;
        color: #1e1e2e;
    }
    .suggestion-empty {
        color: #6c7086;
        text-style: italic;
        padding: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="slash-suggestions")
        self._commands = SLASH_COMMANDS
        self._matches: List[dict] = []
        self._selected: int = -1
        self._item_widgets: List[Static] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="slash-suggestions-empty")

    def update_for(self, text: str) -> None:
        """Recompute matches for the current input text."""
        for child in list(self.children):
            child.remove()
        self._item_widgets = []

        if not text.startswith("/"):
            self._matches = []
            self._selected = -1
            self.add_class("hidden")
            return

        prefix = text[1:].lower().strip()
        if not prefix:
            self._matches = list(self._commands)
        else:
            self._matches = [
                c for c in self._commands
                if c["name"].lower().startswith(prefix)
            ]
        self._matches = self._matches[:8]

        if not self._matches:
            self.mount(Static("no matching commands", classes="suggestion-empty"))
            self.add_class("hidden")
            return

        for cmd in self._matches:
            arg = f" {cmd['arg']}" if cmd.get("arg") else ""
            line = Static(
                f"[#89b4fa]/{cmd['name']}[/]{arg}  [#a6adc8]{cmd['description']}[/]",
                classes="suggestion-item",
                markup=True,
            )
            self.mount(line)
            self._item_widgets.append(line)
        self._selected = 0 if self._matches else -1
        self.remove_class("hidden")
        self._highlight()

    def _highlight(self) -> None:
        for i, widget in enumerate(self._item_widgets):
            widget.set_class(i == self._selected, "selected")

    def move(self, delta: int) -> bool:
        if not self._matches:
            return False
        self._selected = (self._selected + delta) % len(self._matches)
        self._highlight()
        return True

    def apply(self) -> Optional[str]:
        if self._selected < 0 or self._selected >= len(self._matches):
            return None
        cmd = self._matches[self._selected]
        return f"/{cmd['name']} "


class ChatScreen(Screen):
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("escape", "dismiss_suggestions", "Dismiss"),
    ]

    DEFAULT_CSS = TUI_CSS

    def __init__(self) -> None:
        super().__init__()
        self._input: Optional[Input] = None
        self._suggestions: Optional[SlashSuggestions] = None
        self._log: Optional[VerticalScroll] = None
        self._active_thinking: Optional[ThinkingIndicator] = None
        self._active_assistant: Optional[AssistantMessage] = None
        self._active_tools: dict[str, ToolCard] = {}
        self._active_subagent: Optional[SubagentCard] = None
        self._slash_mode: bool = False

    def compose(self) -> ComposeResult:
        app: "IndusBuiltApp" = self.app  # type: ignore[assignment]
        provider = app.runtime_state.get("provider", "")
        model = app.runtime_state.get("model", "")
        with Horizontal(id="chat-header"):
            yield Static(
                f"  [bold #89b4fa]IndusBuilt[/]  ·  [#fab387]{provider}[/] · [#cdd6f4]{model}[/]  ·  press [bold]/[/] for commands",
                id="chat-header-row",
                markup=True,
            )

        yield VerticalScroll(id="chat-log")

        with Container(id="chat-input-container"):
            self._suggestions = SlashSuggestions()
            yield self._suggestions
            self._input = Input(
                placeholder="Type a message, or / for commands…",
                id="chat-input",
            )
            yield self._input
            yield Static(
                "  [bold #fab387]Enter[/] send  ·  [bold #fab387]↑/↓[/] navigate suggestions  ·  "
                "[bold #fab387]Tab[/] apply  ·  [bold #fab387]Esc[/] dismiss  ·  [bold #fab387]Ctrl+C[/] quit",
                id="chat-hint",
                markup=True,
            )

    def on_mount(self) -> None:
        self._input = self.query_one("#chat-input", Input)
        self._suggestions = self.query_one("#slash-suggestions", SlashSuggestions)
        self._log = self.query_one("#chat-log", VerticalScroll)
        self._input.focus()

    def _get_log(self) -> Optional[VerticalScroll]:
        if self._log is None:
            try:
                self._log = self.query_one("#chat-log", VerticalScroll)
            except Exception:
                return None
        return self._log

    def on_screen_resume(self) -> None:
        if self._input is not None:
            self._input.focus()

    # ── Input wiring ────────────────────────────────────────────────────────
    def on_input_changed(self, event: Input.Changed) -> None:
        if self._suggestions is None:
            return
        if event.input.id == "chat-input":
            self._suggestions.update_for(event.value)
            self._slash_mode = event.value.startswith("/")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        text = event.value.strip()
        if not text:
            return
        self._input.value = ""
        if self._suggestions is not None:
            self._suggestions.update_for("")
        self._slash_mode = False
        app: "IndusBuiltApp" = self.app  # type: ignore[assignment]
        app.submit_user_text(text)

    # ── Suggestion navigation ──────────────────────────────────────────────
    def on_key(self, event) -> None:
        if self._input is None or not self._input.has_focus:
            return
        suggestions = self._suggestions
        if suggestions is None or suggestions.has_class("hidden"):
            return
        if not self._slash_mode:
            return
        if event.key == "up":
            if suggestions.move(-1):
                event.prevent_default()
                event.stop()
        elif event.key == "down":
            if suggestions.move(1):
                event.prevent_default()
                event.stop()
        elif event.key == "tab":
            new_text = suggestions.apply()
            if new_text is not None:
                self._input.value = new_text
                self._input.cursor_position = len(new_text)
                suggestions.update_for(new_text)
                event.prevent_default()
                event.stop()

    def action_dismiss_suggestions(self) -> None:
        if self._suggestions is not None and not self._suggestions.has_class("hidden"):
            self._suggestions.update_for("")
            self._slash_mode = False
            return
        if self._input is not None:
            self._input.value = ""
            self._slash_mode = False

    def action_clear_log(self) -> None:
        if self._log is not None:
            for child in list(self._log.children):
                child.remove()

    def action_quit(self) -> None:
        self.app.exit()

    # ── Logging API used by the app event sink ──────────────────────────────
    def _scroll_to_end(self) -> None:
        log = self._get_log()
        if log is not None:
            try:
                log.scroll_end(animate=False)
            except Exception:
                pass

    def _mount(self, widget) -> None:
        log = self._get_log()
        if log is not None:
            try:
                log.mount(widget)
                self._scroll_to_end()
            except Exception:
                pass

    def write_user(self, text: str) -> None:
        self._mount(UserMessage(text))

    def write_assistant_start(self, model: str) -> None:
        self._active_assistant = AssistantMessage(model=model)
        log = self._get_log()
        if log is not None:
            try:
                log.mount(self._active_assistant)
                self._scroll_to_end()
            except Exception:
                pass

    def write_assistant_token(self, token: str) -> None:
        if self._active_assistant is not None:
            self._active_assistant.append_token(token)

    def write_assistant_end(self) -> None:
        if self._active_assistant is not None:
            self._active_assistant.finalize()
        self._active_assistant = None
        self._scroll_to_end()

    def write_thinking(self, label: str) -> None:
        if self._active_thinking is not None:
            return
        self._active_thinking = ThinkingIndicator(label)
        log = self._get_log()
        if log is not None:
            try:
                log.mount(self._active_thinking)
                self._scroll_to_end()
            except Exception:
                pass

    def write_thinking_end(self) -> None:
        if self._active_thinking is not None:
            try:
                self._active_thinking.remove()
            except Exception:
                pass
        self._active_thinking = None
        self._scroll_to_end()

    def write_tool_start(self, call_id: str, name: str, args: dict) -> None:
        card = ToolCard(name, args)
        self._active_tools[call_id] = card
        self._mount(card)

    def write_tool_end(self, call_id: str, name: str, args: dict, result: dict, elapsed: float) -> None:
        card = self._active_tools.pop(call_id, None)
        if card is None:
            return
        card.update_result(result, elapsed)
        self._scroll_to_end()

    def write_code_diff(self, path: str, action: str, before, after, diff_text: Optional[str]) -> None:
        header = Static(
            f"[bold #cba6f7]◆ {action}[/] [#cdd6f4]{path}[/#cdd6f4]",
            id="diff-header",
            markup=True,
        )
        self._mount(header)
        if diff_text:
            try:
                self._mount(CodeBlock(diff_text, language="diff"))
            except Exception:
                pass
        elif after and action == "created_file":
            try:
                preview = after[:1500]
                self._mount(CodeBlock(preview, language=self._guess_language(path)))
            except Exception:
                pass

    @staticmethod
    def _diff_color(action: str) -> str:
        return {
            "created_file": "#a6e3a1",
            "edited": "#89b4fa",
        }.get(action, "#cdd6f4")

    @staticmethod
    def _guess_language(path: str) -> str:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return {
            "py": "python", "js": "javascript", "ts": "typescript",
            "tsx": "tsx", "jsx": "jsx", "json": "json",
            "md": "markdown", "toml": "toml", "yaml": "yaml",
            "yml": "yaml", "html": "html", "css": "css",
            "sh": "bash", "rs": "rust", "go": "go",
        }.get(ext, "text")

    def write_info(self, message: str) -> None:
        for line in message.splitlines() or [""]:
            self._mount(SystemInfo(line))

    def write_success(self, message: str) -> None:
        for line in message.splitlines() or [""]:
            self._mount(SystemSuccess(f"✓ {line}"))

    def write_warning(self, message: str) -> None:
        for line in message.splitlines() or [""]:
            self._mount(SystemWarning(f"⚠ {line}"))

    def write_error(self, message: str) -> None:
        for line in message.splitlines() or [""]:
            self._mount(SystemError(f"✗ {line}"))

    def write_memory(self, status: dict) -> None:
        self._mount(SystemMemory(status))

    def write_hook(self, name: str, event: str, decision: str, reason: str, elapsed: float, error: Optional[str]) -> None:
        self._mount(HookNotice(name, event, decision, reason, elapsed, error))

    def write_subagent_dispatch(self, calls: List[dict]) -> None:
        if not calls:
            return
        if len(calls) == 1:
            task = calls[0].get("task", "")
            card = SubagentCard(calls[0].get("name", ""), task)
            self._active_subagent = card
            self._mount(card)
        else:
            self._mount(SystemInfo(
                f"[bold #cba6f7]◆ dispatching[/] [bold #89b4fa]{len(calls)}[/] [bold #cba6f7]subagents in parallel[/]"
            ))
            for call in calls:
                name = call.get("name", "")
                task = (call.get("task") or "").replace("\n", " ")[:80]
                self._mount(SystemInfo(f"  [#cba6f7]+-- {name}[/] [#a6adc8]{task}[/]"))

    def write_subagent_end(self, name: str, output: str, elapsed: float, turns: int, error: Optional[str], task: str) -> None:
        if self._active_subagent is not None and self._active_subagent._name == name:
            self._active_subagent.update_result(output, elapsed, turns, error)
            self._active_subagent = None
        else:
            card = SubagentCard(name, task)
            self._mount(card)
            card.update_result(output, elapsed, turns, error)
        self._scroll_to_end()
