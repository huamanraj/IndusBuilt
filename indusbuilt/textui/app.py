"""
IndusBuilt TUI application.

Owns the AgentController, the screen stack, and the threading bridge
between the controller's worker thread and the Textual UI thread.
"""
from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive

from .. import __version__
from ..agent import AgentController, run_agent
from ..events import (
    AgentEvent,
    AskChoice,
    AskInput,
    AssistantEnd,
    AssistantStart,
    AssistantToken,
    CodeDiff,
    Error as ErrorEvent,
    HookFired,
    Info,
    MemoryStatus,
    SessionEnd,
    SessionStart,
    SlashHandled,
    SubagentDispatch,
    SubagentEnd,
    Success,
    Thinking,
    ThinkingEnd,
    ToolEnd,
    ToolStart,
    UserEcho,
    Warning,
)
from ..settings import load_settings
from .chat import ChatScreen
from .modals import ChoiceModal, InputModal
from .theme import TUI_CSS
from .welcome import WelcomeScreen


class IndusBuiltApp(App):
    DEFAULT_CSS = TUI_CSS
    TITLE = "IndusBuilt"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
    ]

    version: str = __version__
    controller: AgentController
    runtime_state: Dict[str, str] = {}

    def __init__(self, sandbox_root: Path, settings: Dict[str, Any]) -> None:
        super().__init__()
        self._sandbox_root = sandbox_root
        self._settings = settings
        self._pending_choice_token: Optional[str] = None
        self._pending_input_token: Optional[str] = None
        self._hook_session_started = threading.Event()

    # ── App lifecycle ───────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self.controller = run_agent(
            sandbox_root=self._sandbox_root,
            settings=self._settings,
            sink=self._on_agent_event,
        )
        # Wait briefly for SessionStart so the welcome screen has accurate info.
        # If the controller is fast we get it before mount finishes; if not,
        # the welcome screen re-reads runtime_state when it composes.
        self.runtime_state = {
            "provider": self.controller._refresh_runtime_state()["provider"],
            "model": self.controller._refresh_runtime_state()["model"],
        }
        self.push_screen(WelcomeScreen())

    def on_unmount(self) -> None:
        try:
            self.controller.stop()
        except Exception:
            pass

    # ── Public API for screens ──────────────────────────────────────────────
    def submit_user_text(self, text: str) -> None:
        self.controller.submit_text(text)

    def switch_to_chat(self) -> None:
        if isinstance(self.screen, ChatScreen):
            return
        chat = ChatScreen()
        self.push_screen(chat)
        # Make sure the chat header reflects latest runtime
        try:
            state = self.controller._refresh_runtime_state()
            self.runtime_state = state
        except Exception:
            pass

    # ── Agent event bridge ──────────────────────────────────────────────────
    def _on_agent_event(self, event: AgentEvent) -> None:
        # Worker thread -> UI thread
        self.call_from_thread(self._dispatch_event, event)

    def _dispatch_event(self, event: AgentEvent) -> None:
        if isinstance(event, SessionStart):
            self.runtime_state = {
                "provider": event.provider,
                "model": event.model,
                "subagent_model": event.subagent_model,
            }
            return

        if isinstance(event, SessionEnd):
            self.exit()
            return

        if isinstance(event, UserEcho):
            chat = self._get_chat()
            if chat is not None:
                chat.write_user(event.text)
            return

        if isinstance(event, AssistantStart):
            chat = self._get_chat()
            if chat is not None:
                chat.write_assistant_start(event.model)
            return

        if isinstance(event, AssistantToken):
            chat = self._get_chat()
            if chat is not None:
                chat.write_assistant_token(event.token)
            return

        if isinstance(event, AssistantEnd):
            chat = self._get_chat()
            if chat is not None:
                chat.write_assistant_end()
            return

        if isinstance(event, Thinking):
            chat = self._get_chat()
            if chat is not None:
                chat.write_thinking(event.label)
            return

        if isinstance(event, ThinkingEnd):
            chat = self._get_chat()
            if chat is not None:
                chat.write_thinking_end()
            return

        if isinstance(event, ToolStart):
            chat = self._get_chat()
            if chat is not None:
                chat.write_tool_start(event.call_id, event.tool_name, event.args)
            return

        if isinstance(event, ToolEnd):
            chat = self._get_chat()
            if chat is not None:
                chat.write_tool_end(event.call_id, event.tool_name, event.args, event.result, event.elapsed_s)
            return

        if isinstance(event, CodeDiff):
            chat = self._get_chat()
            if chat is not None:
                chat.write_code_diff(event.path, event.action, event.before, event.after, event.diff_text)
            return

        if isinstance(event, SubagentDispatch):
            chat = self._get_chat()
            if chat is not None:
                chat.write_subagent_dispatch(event.calls)
            return

        if isinstance(event, SubagentEnd):
            chat = self._get_chat()
            if chat is not None:
                chat.write_subagent_end(event.name, event.output, event.elapsed_s, event.turns, event.error, event.task)
            return

        if isinstance(event, HookFired):
            chat = self._get_chat()
            if chat is not None:
                chat.write_hook(event.hook_name, event.event, event.decision, event.reason, event.elapsed_s, event.error)
            return

        if isinstance(event, Info):
            chat = self._get_chat()
            if chat is not None:
                chat.write_info(event.message)
            return

        if isinstance(event, Success):
            chat = self._get_chat()
            if chat is not None:
                chat.write_success(event.message)
            return

        if isinstance(event, Warning):
            chat = self._get_chat()
            if chat is not None:
                chat.write_warning(event.message)
            return

        if isinstance(event, ErrorEvent):
            chat = self._get_chat()
            if chat is not None:
                chat.write_error(event.message)
            return

        if isinstance(event, MemoryStatus):
            chat = self._get_chat()
            if chat is not None:
                chat.write_memory(event.status)
            return

        if isinstance(event, AskChoice):
            self._handle_ask_choice(event)
            return

        if isinstance(event, AskInput):
            self._handle_ask_input(event)
            return

        if isinstance(event, SlashHandled):
            return

    def _get_chat(self) -> Optional[ChatScreen]:
        for screen in self.screen_stack:
            if isinstance(screen, ChatScreen):
                return screen
        return None

    def _handle_ask_choice(self, event: AskChoice) -> None:
        self._pending_choice_token = event.token
        modal = ChoiceModal(event.title, event.options, event.hint)
        self.push_screen(modal, self._on_choice_dismissed)

    def _on_choice_dismissed(self, result: Optional[int]) -> None:
        token = self._pending_choice_token
        self._pending_choice_token = None
        if token is None:
            return
        if result is None or result < 0:
            self.controller.cancel_ask(token)
        else:
            self.controller.submit_choice(token, int(result))

    def _handle_ask_input(self, event: AskInput) -> None:
        self._pending_input_token = event.token
        modal = InputModal(event.title, event.placeholder, event.password)
        self.push_screen(modal, self._on_input_dismissed)

    def _on_input_dismissed(self, result: Optional[str]) -> None:
        token = self._pending_input_token
        self._pending_input_token = None
        if token is None:
            return
        if result is None:
            self.controller.cancel_ask(token)
        else:
            self.controller.submit_input(token, str(result))

    # ── Quit handling ───────────────────────────────────────────────────────
    def action_quit(self) -> None:
        self.exit()


def run(sandbox_root: Path, settings: Dict[str, Any]) -> None:
    app = IndusBuiltApp(sandbox_root=sandbox_root, settings=settings)
    app.run()
