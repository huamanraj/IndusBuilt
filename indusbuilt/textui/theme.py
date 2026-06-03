"""
Catppuccin Mocha palette and Textual CSS for IndusBuilt.
Minimal, dark, modern. Code blocks are the only place we use heavy color.
"""
from __future__ import annotations

# Catppuccin Mocha colors
PALETTE = {
    "base":     "#1e1e2e",
    "mantle":   "#181825",
    "crust":    "#11111b",
    "surface0": "#313244",
    "surface1": "#45475a",
    "surface2": "#585b70",
    "overlay0": "#6c7086",
    "overlay1": "#7f849c",
    "overlay2": "#9399b2",
    "subtext0": "#a6adc8",
    "subtext1": "#bac2de",
    "text":     "#cdd6f4",
    "rosewater":"#f5e0dc",
    "flamingo": "#f2cdcd",
    "pink":     "#f5c2e7",
    "mauve":    "#cba6f7",
    "red":      "#f38ba8",
    "maroon":   "#eba0ac",
    "peach":    "#fab387",
    "yellow":   "#f9e2af",
    "green":    "#a6e3a1",
    "teal":     "#94e2d5",
    "sky":      "#89dceb",
    "sapphire": "#74c7ec",
    "blue":     "#89b4fa",
    "lavender": "#b4befe",
}

# Semantic role names (used in CSS via $name)
SEMANTIC_CSS = """
$primary: #89b4fa;
$secondary: #cba6f7;
$accent: #fab387;
$warning: #f9e2af;
$error: #f38ba8;
$success: #a6e3a1;
$text: #cdd6f4;
$text-muted: #a6adc8;
$background: #1e1e2e;
$background-alt: #181825;
$surface: #313244;
$surface-alt: #45475a;
$border: #45475a;
$border-active: #89b4fa;
"""


# Note: Textual only supports a fixed set of theme variables
# ($primary/$secondary/$accent/$background/$surface/$panel/$boost/
#  $warning/$error/$success/$text/$text-muted/$text-disabled/$border).
# We override the standard ones with Catppuccin Mocha colors, and use
# direct hex values for everything else.
TUI_CSS = """
Screen {
    background: #1e1e2e;
    color: #cdd6f4;
}

#welcome-container {
    align: center middle;
    width: 100%;
    height: 100%;
}

#welcome-card {
    width: 78;
    height: auto;
    padding: 2 4;
    background: #181825;
    border: round #45475a;
}

#welcome-banner {
    color: #89b4fa;
    text-align: center;
    text-style: bold;
    height: auto;
    margin-bottom: 1;
}

#welcome-subtitle {
    color: #a6adc8;
    text-align: center;
    text-style: italic;
    margin-bottom: 1;
}

#welcome-divider {
    color: #45475a;
    height: 1;
    margin-bottom: 1;
}

#welcome-info {
    color: #a6adc8;
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
    background: #313244;
    border: tall #45475a;
    height: 3;
    color: #cdd6f4;
}

#welcome-input:focus {
    border: tall #89b4fa;
}

#welcome-footer {
    dock: bottom;
    height: 1;
    padding: 0 2;
    color: #a6adc8;
}

#welcome-footer-version {
    color: #fab387;
    text-style: bold;
}

#welcome-footer-hint {
    color: #a6adc8;
}

#chat-header {
    dock: top;
    height: 1;
    background: #181825;
    color: #a6adc8;
    padding: 0 2;
}

#chat-log {
    background: #1e1e2e;
    padding: 0 2;
    scrollbar-gutter: stable;
}

#chat-input-container {
    dock: bottom;
    height: 7;
    background: #181825;
    padding: 0 2 1 2;
    border-top: solid #45475a;
}

#chat-input {
    background: #313244;
    border: tall #45475a;
    height: 3;
    color: #cdd6f4;
}

#chat-input:focus {
    border: tall #89b4fa;
}

#chat-hint {
    color: #a6adc8;
    height: 1;
    padding: 0 1;
}

#slash-suggestions {
    display: none;
    background: #313244;
    border: round #45475a;
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

SystemInfo {
    color: #a6adc8;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

SystemSuccess {
    color: #a6e3a1;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

SystemWarning {
    color: #f9e2af;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

SystemError {
    color: #f38ba8;
    padding: 0 2;
    margin: 1 0;
    height: auto;
}

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

CodeBlock {
    background: #181825;
    border: round #45475a;
    padding: 0 1;
    margin: 0 0 1 0;
    height: auto;
    max-height: 20;
    overflow-y: auto;
}

HookNotice {
    background: transparent;
    color: #a6adc8;
    padding: 0 2;
    margin: 0;
    height: 1;
}

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

.tool-args {
    color: #a6adc8;
    margin: 0 0 1 0;
}

.tool-result {
    color: #a6adc8;
    margin: 0;
}

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

.subagent-task {
    color: #a6adc8;
    margin: 0 0 1 0;
}

.subagent-output {
    color: #cdd6f4;
    margin: 0 0 1 0;
}

ThinkingIndicator {
    background: transparent;
    color: #89b4fa;
    padding: 0 2;
    height: 1;
}

ModalScreen {
    background: #11111b 80%;
    align: center middle;
}

#modal-container {
    width: 70;
    height: auto;
    max-height: 80%;
    background: #181825;
    border: round #89b4fa;
    padding: 1 2;
}

#modal-title {
    color: #89b4fa;
    text-style: bold;
    height: 1;
    margin-bottom: 1;
}

#modal-hint {
    color: #a6adc8;
    height: 1;
    margin-bottom: 1;
}

#modal-list {
    height: auto;
    max-height: 20;
    background: #313244;
    border: round #45475a;
}

#modal-list > ListItem {
    padding: 0 1;
}

#modal-list > ListItem.--highlight {
    background: #89b4fa;
    color: #1e1e2e;
}

#modal-list > ListItem.--highlight Label {
    color: #1e1e2e;
}

#modal-input {
    background: #313244;
    border: tall #89b4fa;
    height: 3;
    color: #cdd6f4;
    margin-top: 1;
}
"""
