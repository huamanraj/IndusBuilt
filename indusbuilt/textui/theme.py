"""
Pure grayscale palette and Textual CSS for IndusBuilt.

No color hues. All visual differentiation is expressed through
brightness: near-black for backgrounds, mid-grey for muted text,
and off-white / white for active and emphasized content.
"""
from __future__ import annotations

# Pure grayscale ladder (dark -> light)
PALETTE = {
    "base":     "#0a0a0a",  # screen background (near black)
    "mantle":   "#111111",  # panel / header background
    "crust":    "#000000",  # deepest (modal scrim)
    "surface0": "#1a1a1a",  # card background
    "surface1": "#222222",  # input field background
    "surface2": "#2e2e2e",  # raised surface
    "overlay0": "#5e5e5e",  # disabled / hint
    "overlay1": "#7a7a7a",  # secondary muted
    "overlay2": "#9a9a9a",  # primary muted
    "subtext0": "#a8a8a8",  # body muted
    "subtext1": "#c0c0c0",  # body secondary
    "text":     "#e6e6e6",  # body text (off-white)
    "white":    "#ffffff",  # accent / highlight
    # Legacy keys kept as aliases pointing to grayscale values so any
    # template / linter that still references them stays valid.
    "rosewater":"#e6e6e6",
    "flamingo": "#c0c0c0",
    "pink":     "#a8a8a8",
    "mauve":    "#c0c0c0",
    "red":      "#c0c0c0",
    "maroon":   "#a8a8a8",
    "peach":    "#e6e6e6",
    "yellow":   "#c0c0c0",
    "green":    "#c0c0c0",
    "teal":     "#a8a8a8",
    "sky":      "#c0c0c0",
    "sapphire": "#a8a8a8",
    "blue":     "#e6e6e6",
    "lavender": "#c0c0c0",
}

# Semantic role names (used in CSS via $name) - all grayscale.
SEMANTIC_CSS = """
$primary: #e6e6e6;
$secondary: #c0c0c0;
$accent: #ffffff;
$warning: #c0c0c0;
$error: #c0c0c0;
$success: #c0c0c0;
$text: #e6e6e6;
$text-muted: #a8a8a8;
$background: #0a0a0a;
$background-alt: #111111;
$surface: #1a1a1a;
$surface-alt: #222222;
$border: #2e2e2e;
$border-active: #ffffff;
"""


# Note: Textual only supports a fixed set of theme variables
# ($primary/$secondary/$accent/$background/$surface/$panel/$boost/
#  $warning/$error/$success/$text/$text-muted/$text-disabled/$border).
# We override the standard ones with grayscale values, and use
# direct hex values for everything else.
TUI_CSS = """
Screen {
    background: #0a0a0a;
    color: #e6e6e6;
}

#welcome-container {
    align: center middle;
    width: 100%;
    height: 100%;
}

#welcome-card {
    width: 108;
    height: auto;
    padding: 2 4;
    background: #111111;
    border: round #2e2e2e;
}

#welcome-banner {
    color: #ffffff;
    text-align: center;
    text-style: bold;
    height: auto;
    margin-bottom: 1;
    width: 100%;
}

#welcome-subtitle {
    color: #a8a8a8;
    text-align: center;
    text-style: italic;
    margin-bottom: 1;
}

#welcome-divider {
    color: #2e2e2e;
    height: 1;
    margin-bottom: 1;
}

#welcome-info {
    color: #a8a8a8;
    text-align: center;
    height: auto;
    margin-bottom: 1;
}

#welcome-input-container {
    height: 5;
    margin-top: 1;
    padding: 0 2;
}

#welcome-input {
    background: #1a1a1a;
    border: tall #2e2e2e;
    height: 3;
    color: #e6e6e6;
}

#welcome-input:focus {
    border: tall #ffffff;
}

#welcome-footer {
    dock: bottom;
    height: 1;
    padding: 0 2;
    color: #a8a8a8;
}

#welcome-footer-version {
    color: #e6e6e6;
    text-style: bold;
}

#welcome-footer-hint {
    color: #a8a8a8;
}

#chat-header {
    dock: top;
    height: 1;
    background: #111111;
    color: #a8a8a8;
    padding: 0 2;
}

#chat-log {
    background: #0a0a0a;
    padding: 0 2;
    scrollbar-gutter: stable;
}

#chat-input-container {
    dock: bottom;
    height: 7;
    background: #111111;
    padding: 0 2 1 2;
    border-top: solid #2e2e2e;
}

#chat-input {
    background: #1a1a1a;
    border: tall #2e2e2e;
    height: 3;
    color: #e6e6e6;
}

#chat-input:focus {
    border: tall #ffffff;
}

#chat-hint {
    color: #7a7a7a;
    height: 1;
    padding: 0 1;
}

#slash-suggestions {
    display: none;
    background: #1a1a1a;
    border: round #ffffff;
    height: auto;
    max-height: 12;
    margin: 0 2;
    padding: 0 1;
}

#slash-suggestions.active {
    display: block;
}

.suggestion-item {
    height: 1;
    padding: 0 1;
    color: #e6e6e6;
}

.suggestion-item.selected {
    background: #ffffff;
    color: #0a0a0a;
    text-style: bold;
}

.suggestion-empty {
    color: #5e5e5e;
    text-style: italic;
    padding: 1;
}

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

SystemInfo {
    color: #a8a8a8;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

SystemSuccess {
    color: #ffffff;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

SystemWarning {
    color: #c0c0c0;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

SystemError {
    color: #ffffff;
    text-style: bold;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

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

CodeBlock {
    background: #111111;
    border: round #2e2e2e;
    padding: 0 1;
    margin: 0 0 1 0;
    height: auto;
    max-height: 20;
    overflow-y: auto;
}

HookNotice {
    background: transparent;
    color: #a8a8a8;
    padding: 0 2;
    margin: 0;
    height: 1;
}

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

.tool-args {
    color: #a8a8a8;
    margin: 0 0 1 0;
}

.tool-result {
    color: #a8a8a8;
    margin: 0;
}

SubagentCard {
    background: #1a1a1a;
    border: round #ffffff;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

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
    color: #ffffff;
    text-style: bold;
    height: 1;
}
.term-cmd {
    color: #e6e6e6;
    background: #111111;
    padding: 0 1;
    margin: 0 0 1 0;
    height: auto;
}
.term-status {
    color: #a8a8a8;
    margin: 0 0 1 0;
    height: 1;
}
.term-status.error {
    color: #ffffff;
    text-style: bold;
}
.term-status.ok {
    color: #c0c0c0;
}
.term-section-label {
    color: #7a7a7a;
    text-style: italic;
    height: 1;
}
.term-output {
    color: #e6e6e6;
    background: #111111;
    padding: 0 1;
    margin: 0 0 1 0;
    height: auto;
    max-height: 18;
    overflow-y: auto;
}
.term-output-empty {
    color: #5e5e5e;
    text-style: italic;
}

.subagent-header {
    color: #ffffff;
    text-style: bold;
    height: 1;
}

.subagent-task {
    color: #a8a8a8;
    margin: 0 0 1 0;
}

.subagent-output {
    color: #e6e6e6;
    margin: 0 0 1 0;
}

ThinkingIndicator {
    background: transparent;
    color: #ffffff;
    padding: 0 2;
    height: 1;
}

ModalScreen {
    background: #000000 80%;
    align: center middle;
}

#modal-container {
    width: 70;
    height: auto;
    max-height: 80%;
    background: #111111;
    border: round #ffffff;
    padding: 1 2;
}

#modal-title {
    color: #ffffff;
    text-style: bold;
    height: 1;
    margin-bottom: 1;
}

#modal-hint {
    color: #a8a8a8;
    height: 1;
    margin-bottom: 1;
}

#modal-list {
    height: auto;
    max-height: 20;
    background: #1a1a1a;
    border: round #2e2e2e;
}

#modal-list > ListItem {
    padding: 0 1;
}

#modal-list > ListItem.--highlight {
    background: #ffffff;
    color: #0a0a0a;
}

#modal-list > ListItem.--highlight Label {
    color: #0a0a0a;
}

#modal-input {
    background: #1a1a1a;
    border: tall #ffffff;
    height: 3;
    color: #e6e6e6;
    margin-top: 1;
}
"""
