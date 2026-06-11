"""
Welcome screen — the initial landing page.
ASCII banner on top, agent info in the middle, centered input below,
version and hint pinned to the bottom corners.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, Static

from .theme import TUI_CSS
from .banner import render_banner

if TYPE_CHECKING:
    from .app import IndusBuiltApp


class WelcomeScreen(Screen):
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
    ]

    DEFAULT_CSS = TUI_CSS

    def __init__(self) -> None:
        super().__init__()
        self._input_widget: Input | None = None

    def compose(self) -> ComposeResult:
        app: "IndusBuiltApp" = self.app  # type: ignore[assignment]
        sandbox = str(app.controller.sandbox_root)
        provider = app.runtime_state.get("provider", "openai")
        model = app.runtime_state.get("model", "")
        version = app.version

        with Container(id="welcome-container"):
            with Container(id="welcome-card"):
                yield Static(render_banner(), id="welcome-banner", markup=False)
                yield Static("The fastest coding agent", id="welcome-subtitle", markup=False)
                yield Static("─" * 70, id="welcome-divider", markup=False)
                yield Static(
                    f"  [bold #e6e6e6]provider[/bold #e6e6e6] [#e6e6e6]{provider}[/#e6e6e6]"
                    f"      [bold #e6e6e6]model[/bold #e6e6e6] [#c0c0c0]{model}[/#c0c0c0]",
                    id="welcome-info-provider",
                    markup=True,
                )
                yield Static(
                    f"  [bold #e6e6e6]sandbox[/bold #e6e6e6] [#a8a8a8]{sandbox}[/#a8a8a8]",
                    id="welcome-info-sandbox",
                    markup=True,
                )
                with Container(id="welcome-input-container"):
                    yield Input(
                        placeholder="Ask IndusBuilt anything, or press / for commands…",
                        id="welcome-input",
                    )

        with Horizontal(id="welcome-footer"):
            yield Static(f"IndusBuilt v{version}", id="welcome-footer-version")
            yield Static(
                "  ·  press [bold]Enter[/bold] to send  ·  [bold]/[/bold] for commands  ·  [bold]Ctrl+C[/bold] to quit",
                id="welcome-footer-hint",
                markup=True,
            )

    def on_mount(self) -> None:
        self._input_widget = self.query_one("#welcome-input", Input)
        self._input_widget.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self._input_widget.value = ""
        app: "IndusBuiltApp" = self.app  # type: ignore[assignment]
        app.submit_user_text(text)
        app.switch_to_chat()

    def action_quit(self) -> None:
        self.app.exit()
