"""
Event types emitted by the AgentController to its UI sink.

The controller is UI-agnostic: it never prints, never calls input(),
never spins a terminal cursor. Instead it pushes AgentEvent instances
to a sink callable. The Textual app subscribes to those events and
renders them however it likes.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class AgentEvent:
    """Base class for all controller events."""


@dataclass
class SessionStart(AgentEvent):
    provider: str
    model: str
    subagent_model: str
    sandbox: str
    skills: List[str] = field(default_factory=list)
    subagents: List[str] = field(default_factory=list)
    hooks: List[str] = field(default_factory=list)
    router_enabled: bool = True
    router_provider: str = ""
    router_model: str = ""


@dataclass
class SessionEnd(AgentEvent):
    reason: str = "exit"


@dataclass
class UserEcho(AgentEvent):
    text: str


@dataclass
class AssistantStart(AgentEvent):
    model: str


@dataclass
class AssistantToken(AgentEvent):
    token: str


@dataclass
class AssistantEnd(AgentEvent):
    full_text: str
    had_tool_calls: bool = False
    error: Optional[str] = None


@dataclass
class Thinking(AgentEvent):
    label: str = "thinking"


@dataclass
class ThinkingEnd(AgentEvent):
    pass


@dataclass
class ToolStart(AgentEvent):
    call_id: str
    tool_name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolEnd(AgentEvent):
    call_id: str
    tool_name: str
    args: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0


@dataclass
class SubagentDispatch(AgentEvent):
    calls: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class SubagentEnd(AgentEvent):
    name: str
    output: str
    elapsed_s: float
    turns: int
    error: Optional[str] = None
    task: str = ""


@dataclass
class HookFired(AgentEvent):
    hook_name: str
    event: str
    decision: str
    reason: str = ""
    elapsed_s: float = 0.0
    error: Optional[str] = None


@dataclass
class Info(AgentEvent):
    message: str


@dataclass
class Success(AgentEvent):
    message: str


@dataclass
class Warning(AgentEvent):
    message: str


@dataclass
class Error(AgentEvent):
    message: str


@dataclass
class MemoryStatus(AgentEvent):
    status: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeDiff(AgentEvent):
    """A code change detected from a tool result. Used for colored highlighting."""
    path: str
    action: str  # created_file / edited / offloaded_large_output
    before: Optional[str] = None
    after: Optional[str] = None
    diff_text: Optional[str] = None


@dataclass
class AskChoice(AgentEvent):
    """UI should show a selection prompt and call submit_choice on the controller."""
    token: str
    title: str
    options: List[str]
    hint: str = "Use ↑/↓ and Enter"
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)


@dataclass
class AskInput(AgentEvent):
    """UI should show an input prompt and call submit_input on the controller."""
    token: str
    title: str
    placeholder: str = ""
    password: bool = False
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)


@dataclass
class SlashHandled(AgentEvent):
    """A slash command was executed; the loop should continue."""
    pass


@dataclass
class RouterDecision(AgentEvent):
    """The router model selected a set of tool categories for the current turn."""
    user_message: str
    categories: List[str] = field(default_factory=list)
    tool_names: List[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    elapsed_s: float = 0.0
    error: Optional[str] = None
    rerouted: bool = False


EventSink = Callable[[AgentEvent], None]
