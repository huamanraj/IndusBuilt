"""
Modal screens for choices, freeform input, and the slash-command palette.
"""
from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from .theme import TUI_CSS


# ── Choice modal: pick one option from a list ──────────────────────────────
class ChoiceModal(ModalScreen[int]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
    ]

    DEFAULT_CSS = TUI_CSS

    def __init__(self, title: str, options: List[str], hint: str = "Use ↑/↓ and Enter") -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Container(id="modal-container"):
            yield Static(self._title, id="modal-title")
            yield Static(self._hint, id="modal-hint")
            items = [ListItem(Label(opt), id=f"opt-{i}") for i, opt in enumerate(self._options)]
            yield ListView(*items, id="modal-list")

    def on_mount(self) -> None:
        list_view = self.query_one("#modal-list", ListView)
        list_view.focus()
        if self._options:
            list_view.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index if event.list_view.index is not None else 0
        self.dismiss(int(idx))

    def action_select(self) -> None:
        list_view = self.query_one("#modal-list", ListView)
        idx = list_view.index if list_view.index is not None else 0
        self.dismiss(int(idx))

    def action_cancel(self) -> None:
        self.dismiss(-1)


# ── Input modal: freeform text (API key etc.) ──────────────────────────────
class InputModal(ModalScreen[Optional[str]]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = TUI_CSS

    def __init__(self, title: str, placeholder: str = "", password: bool = False) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._password = password

    def compose(self) -> ComposeResult:
        with Container(id="modal-container"):
            yield Static(self._title, id="modal-title")
            yield Static("Enter to submit · Esc to cancel", id="modal-hint")
            yield Input(
                placeholder=self._placeholder,
                password=self._password,
                id="modal-input",
            )

    def on_mount(self) -> None:
        self.query_one("#modal-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
