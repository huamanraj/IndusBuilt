"""
IndusBuilt Coding Agent Core.

UI-agnostic: never prints, never calls input(). Pushes AgentEvent
instances to an injected sink so any frontend (currently the Textual
TUI) can render them.
"""
from __future__ import annotations

import concurrent.futures
import getpass
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from litellm import completion

from .context_manager import ContextManager
from .events import (
    AgentEvent,
    AskChoice,
    AskInput,
    AssistantEnd,
    AssistantStart,
    AssistantToken,
    Error as ErrorEvent,
    EventSink,
    HookFired,
    Info,
    MemoryStatus,
    RouterDecision,
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
from .hook_commands import HookCommandHandler
from .hooks import HookEvent, HookEventContext, HookRegistry
from .settings import (
    MODEL_CHOICES,
    PROVIDERS,
    ROUTER_MODEL_CHOICES,
    SUBAGENT_MODEL_CHOICES,
    get_active_provider,
    get_api_key,
    get_model,
    get_router_enabled,
    get_router_model,
    get_router_provider,
    get_subagent_model,
    save_settings,
    set_active_provider,
    set_api_key,
    set_model,
    set_router_enabled,
    set_router_model,
    set_router_provider,
    set_subagent_model,
)
from .skill_commands import SkillCommandHandler
from .skills import SkillRegistry
from .subagent_commands import SubAgentCommandHandler
from .subagents import SubAgentRegistry, run_subagents_parallel


# ── Sandbox enforcement ───────────────────────────────────────────────────────
def resolve_sandboxed_path(path_str: str, sandbox_root: Path) -> Path:
    """
    Resolve a path and enforce it stays within sandbox_root.
    Raises ValueError if the resolved path would escape the sandbox.
    """
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = (sandbox_root / path).resolve()
    else:
        path = path.resolve()

    try:
        path.relative_to(sandbox_root)
    except ValueError:
        raise ValueError(
            f"Access denied: '{path}' is outside the sandbox directory '{sandbox_root}'."
        )
    return path


# ── Tools ─────────────────────────────────────────────────────────────────────
def make_tools(
    sandbox_root: Path,
    skill_registry: Optional[SkillRegistry] = None,
    context_manager: Optional[ContextManager] = None,
    conversation_ref: Optional[List[Dict[str, Any]]] = None,
):
    """Returns the core tools bound to a sandbox_root.

    As of v2.4.0 the agent is shell-first: the only direct tool is `terminal`.
    All file inspection, navigation, searching, and editing must be done via
    shell one-liners (cat, ls, find, grep -rn, sed, python -c, etc.) so the
    agent can combine steps and stay token-efficient.
    """

    def terminal_tool(
        command: str,
        cwd: Optional[str] = None,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """Run a shell command inside the sandbox and return its output.

        Use this for ANY task that is faster / cheaper as a shell one-liner
        instead of chaining dedicated tools. Examples:
          - `dir` / `ls -la` to list a directory
          - `python -c "..."` to evaluate a snippet
          - `find . -name "*.py" | head -50` to find files
          - `cat file.py` for a quick peek at a small file
          - `git status`, `git diff`, `git log -10`
          - `pip list`, `node -v`, etc.

        The command is executed with `cwd` resolved under the sandbox.
        Output is captured (stdout + stderr) and truncated to roughly
        MAX_TERMINAL_CHARS to keep the agent context small. On truncation
        the response says so and points you at the offloaded file.
        """
        MAX_TERMINAL_CHARS = 8000
        MAX_TIMEOUT = 300

        try:
            if not isinstance(command, str) or not command.strip():
                return {"error": "terminal: 'command' must be a non-empty string."}
            effective_timeout = max(1, min(int(timeout), MAX_TIMEOUT))

            if cwd:
                workdir = resolve_sandboxed_path(cwd, sandbox_root)
            else:
                workdir = sandbox_root

            if not workdir.is_dir():
                return {"error": f"terminal: cwd is not a directory: {workdir}"}

            use_shell = os.name == "nt"
            start = time.time()
            try:
                proc = subprocess.run(
                    command if use_shell else ["bash", "-lc", command],
                    cwd=str(workdir),
                    shell=use_shell,
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "error": f"terminal: command timed out after {effective_timeout}s.",
                    "command": command,
                    "cwd": str(workdir),
                    "partial_stdout": (exc.stdout or "")[:MAX_TERMINAL_CHARS] if isinstance(exc.stdout, str) else "",
                }
            except FileNotFoundError as exc:
                return {"error": f"terminal: required executable not found ({exc})."}
            except Exception as exc:
                return {"error": f"terminal: failed to launch: {exc}"}

            elapsed = time.time() - start
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            truncated_stdout = False
            truncated_stderr = False

            if len(stdout) > MAX_TERMINAL_CHARS:
                offload_dir = sandbox_root / ".indusbuilt" / "terminal_offload"
                offload_dir.mkdir(parents=True, exist_ok=True)
                offload_path = offload_dir / f"terminal_{uuid.uuid4().hex[:8]}.log"
                offload_path.write_text(stdout, encoding="utf-8", errors="replace")
                stdout = stdout[:MAX_TERMINAL_CHARS] + (
                    f"\n... [truncated, full output saved to {offload_path}]"
                )
                truncated_stdout = True
            if len(stderr) > MAX_TERMINAL_CHARS:
                stderr = stderr[:MAX_TERMINAL_CHARS] + "\n... [truncated]"

            result: Dict[str, Any] = {
                "command": command,
                "cwd": str(workdir),
                "exit_code": proc.returncode,
                "elapsed_s": round(elapsed, 2),
                "stdout": stdout,
                "stderr": stderr,
                "stdout_chars": len(proc.stdout or ""),
                "stderr_chars": len(proc.stderr or ""),
            }
            if truncated_stdout:
                result["truncated"] = True
            if proc.returncode != 0:
                result["error"] = f"terminal: command exited with code {proc.returncode}."
            return result
        except Exception as exc:
            return {"error": f"terminal: unexpected error: {exc}"}

    tools = {
        "terminal":     terminal_tool,
    }

    if skill_registry is not None:
        tools["activate_skill"] = skill_registry.activate

    if context_manager is not None:
        tools["save_memory"] = context_manager.save_memory
        tools["search_memory"] = context_manager.search_memory
        tools["retrieve_code"] = context_manager.retrieve_code
        tools["offload_large_output"] = context_manager.offload_large_output

        def summarize_session_tool(reason: str = "") -> Dict[str, Any]:
            return context_manager.summarize_session(conversation_ref or [], reason=reason or None)

        tools["summarize_session"] = summarize_session_tool

    return tools


# ── OpenAI tool schemas ───────────────────────────────────────────────────────
CORE_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "Execute a shell command inside the sandbox and return its output. "
                "This is the agent's PRIMARY (and only direct) tool. Use it for everything:\n"
                "  - listing or exploring the project:   `ls -la` / `dir`, `find . -name '*.py' | head -50`\n"
                "  - reading files:                     `cat file.py` (small) or `sed -n '1,200p' file.py` (paged)\n"
                "  - searching content:                 `grep -rn 'pattern' src/ --include='*.py'`\n"
                "  - editing / creating files:          `python -c 'open(\"f.py\",\"w\").write(...)'`, "
                "                                        `sed -i 's/old/new/g' file`, or `printf ... > file`\n"
                "  - running tests/builds/scripts:      `pytest -x -q`, `npm test`, `python -m build`\n"
                "  - inspecting the environment:        `git status`, `git diff`, `pip list`, `node -v`\n"
                "Combine steps with `&&` / `;` / pipes so a single terminal call replaces many separate tool "
                "calls. Output is truncated to ~8000 chars; longer output is saved under "
                ".indusbuilt/terminal_offload/ and the path is returned in the result. "
                "The command is sandboxed to the project root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run. On Windows uses cmd.exe; on POSIX uses bash -lc."
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory (relative to sandbox). Defaults to the sandbox root."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Defaults to 60, max 300.",
                        "default": 60
                    }
                },
                "required": ["command"]
            }
        }
    }
]

MEMORY_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Store important knowledge in local markdown memory files and index it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "description": "Type category, e.g. bug, decision, architecture."},
                    "topic": {"type": "string", "description": "Topic slug or short topic name."},
                    "summary": {"type": "string", "description": "Short summary to store and index."},
                    "content": {"type": "string", "description": "Optional detailed notes."},
                },
                "required": ["memory_type", "topic", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search previously stored local memory and return only relevant summaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {"type": "integer", "description": "Maximum result count.", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_session",
            "description": "Compress current conversation context into a rolling session summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Optional reason for summarization."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_code",
            "description": "Retrieve relevant code snippets for a query from the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What code to retrieve."},
                    "limit": {"type": "integer", "description": "Maximum snippets to return.", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "offload_large_output",
            "description": "Persist large output to disk and keep only a compact preview in context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Label for the saved output."},
                    "content": {"type": "string", "description": "Large output content to offload."},
                },
                "required": ["name", "content"],
            },
        },
    },
]


def build_openai_tools(
    skill_registry: Optional[SkillRegistry] = None,
    context_manager: Optional[ContextManager] = None,
    subagent_registry: Optional[SubAgentRegistry] = None,
) -> List[Dict[str, Any]]:
    tools = list(CORE_OPENAI_TOOLS)
    if skill_registry is not None and skill_registry.skills:
        tools.append(skill_registry.activation_tool_schema())
    if context_manager is not None:
        tools.extend(MEMORY_OPENAI_TOOLS)
    if subagent_registry is not None and subagent_registry.list_agents():
        tools.append(subagent_registry.call_schema())
    return tools


# ── Tool categories + router ──────────────────────────────────────────────────
# v2.4.0: the only direct tool is `terminal`. Router categories now map onto
# the optional capability tools (memory, skills, subagents) plus the always-on
# terminal. The router's job is reduced to deciding which OPT-IN tool groups
# to expose for the current turn.
TOOL_CATEGORIES: Dict[str, List[str]] = {
    "terminal": ["terminal"],
    "memory": ["save_memory", "search_memory", "retrieve_code", "summarize_session", "offload_large_output"],
    "skills": ["activate_skill"],
    "subagents": ["call_subagent"],
}

CATEGORY_DESCRIPTIONS: Dict[str, str] = {
    "terminal": "Execute shell commands inside the sandbox (the agent's primary tool).",
    "memory": "Persist, search, and recall long-term knowledge across sessions.",
    "skills": "Load specialized instruction sets (skills) into the agent context.",
    "subagents": "Delegate research/analysis tasks to specialized subagents (run in parallel).",
}

CATEGORY_ORDER: List[str] = [
    "terminal",
    "memory",
    "subagents",
    "skills",
]

ROUTER_SYSTEM_PROMPT = """You are a tool router for an AI coding agent.

Your job: look at the user's message and pick the MINIMUM set of OPT-IN tool
categories the main agent will need. The agent always has the `terminal` tool
(shell access) regardless of what you return — your decision only governs the
optional capability tools (memory, skills, subagents). This keeps costs and
latency down.

Available opt-in categories:
{categories}

Rules:
- For simple chat (greetings, explanations, theory questions with no file/code work),
  return an empty list. The main agent can answer without any opt-in tools.
- For almost every coding task (reading files, searching, editing, running
  commands, debugging, refactoring) the agent only needs `terminal` — do NOT
  return `terminal`; it is always on. Return an empty list.
- Add "memory" only if the user asks to remember, recall, or persist something
  across sessions.
- Add "subagents" only if the task clearly benefits from parallel research
  (e.g. "explore the repo and summarize modules", "compare how X is done in
  several places"). A normal "fix this bug" does NOT need subagents.
- Add "skills" only if the user explicitly asks for a known skill or the task
  is a clear match for a loaded skill.
- Prefer an empty list over speculative categories. If unsure, return [].

Respond with ONLY a JSON object, no prose, no markdown fences:
{{"categories": ["subagents"]}}
"""


def categories_to_tool_names(categories: List[str]) -> List[str]:
    """Expand category names to the underlying tool names, preserving order and dedup."""
    seen: set = set()
    out: List[str] = []
    for cat in categories:
        for tool_name in TOOL_CATEGORIES.get(cat, []):
            if tool_name not in seen:
                seen.add(tool_name)
                out.append(tool_name)
    return out


def filter_tools(
    tools: List[Dict[str, Any]],
    allowed_names: Optional[set],
) -> List[Dict[str, Any]]:
    """Return only tools whose function name is in allowed_names. None = keep all."""
    if allowed_names is None:
        return tools
    return [t for t in tools if t.get("function", {}).get("name") in allowed_names]


def parse_router_response(text: str) -> List[str]:
    """Extract the categories list from a router model response.

    Tolerates prose, markdown fences, and partial JSON. Falls back to scanning
    the text for known category names if no JSON object is found.
    """
    if not text:
        return []
    candidate = text.strip()

    # Strip markdown code fences if present.
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    # Try to find a JSON object containing a "categories" key.
    import re
    match = re.search(r"\{[^{}]*\"categories\"[^{}]*\[.*?\][^{}]*\}", candidate, re.DOTALL)
    if not match:
        match = re.search(r"\{.*?\"categories\".*?\}", candidate, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            cats = data.get("categories", [])
            if isinstance(cats, list):
                return [str(c).strip() for c in cats if isinstance(c, (str, int))]
        except Exception:
            pass

    # Last-resort fallback: substring match for known category names.
    lowered = candidate.lower()
    found: List[str] = []
    for cat in TOOL_CATEGORIES:
        if cat in lowered and cat not in found:
            found.append(cat)
    return found


# ── System prompt ─────────────────────────────────────────────────────────────
_CATEGORY_SECTION_TEXT: Dict[str, str] = {
    "terminal": (
        "TERMINAL (your only direct tool — use it for everything):\n"
        "- terminal – run a shell command inside the sandbox. Use it for `ls`/`dir`, `cat`/`type`, `find`, "
        "`grep -rn`, `sed`, `git`, `python -c`, `pytest`, `npm`, `pip`, etc. Combine steps with `&&` or `;` "
        "or pipes so a single call replaces many separate calls. cwd defaults to the sandbox root."
    ),
    "memory": (
        "MEMORY (opt-in):\n"
        "- save_memory, search_memory, retrieve_code, summarize_session, offload_large_output"
    ),
    "subagents": (
        "SUBAGENTS (opt-in):\n"
        "- call_subagent – delegate tasks to specialized subagents (call multiple times to run in parallel)"
    ),
    "skills": (
        "SKILLS (opt-in):\n"
        "- activate_skill – load skill instructions when available skills match the task"
    ),
}

_SUBAGENTS_BEHAVIORAL_PARAGRAPH = """
SUBAGENTS:
- Use call_subagent to delegate research, exploration, or analysis to a specialized agent.
- Call call_subagent multiple times in the same response to run subagents IN PARALLEL — all run
  concurrently and their results are returned together before you continue.
- Use subagents when you need to gather context from multiple parts of the codebase at once,
  or when exploration and analysis can happen independently before you act.
"""


_TERMINAL_AWARE_PARAGRAPH = """
TERMINAL IS YOUR ONLY DIRECT TOOL:
- `terminal` runs a shell command (bash on POSIX, cmd.exe on Windows) inside the sandbox.
- It is the ONLY way you can read, list, search, or modify files. There are no dedicated
  read_file / edit_file / list_files / grep tools. Use shell one-liners for all of them.

Common recipes:
  - listing or exploring the tree:        `ls -la` / `dir`, `find . -name "*.py" | head -50`
  - quick file peek:                      `cat path/to/file.py` (small files only)
  - paged file read:                      `sed -n '1,200p' path/to/file.py` or `head -n 200 file.py`
  - content search:                       `grep -rn "def foo" src/ --include="*.py"`
  - file name search:                     `find . -name "*.py" -not -path "*/.venv/*"`
  - apply a string replace:               `python -c "import pathlib; p=pathlib.Path('f'); p.write_text(p.read_text().replace('old','new'))"`
  - in-place sed:                         `sed -i 's/old/new/g' file` (POSIX) or `python -c "..."` (Windows)
  - create a new file:                    `printf 'content' > new_file` or `python -c "open('f','w').write('content')"`
  - git status / diff / log:              `git status && git diff --stat`
  - environment / tool versions:          `python --version && pip list | head -30`
  - running a script or test:             `pytest -x -q tests/`

Combine steps with `&&`, `;`, or pipes so a single terminal call replaces many separate calls.
The `terminal` tool's stdout/stderr are captured and truncated to ~8000 chars; anything longer is
saved to `.indusbuilt/terminal_offload/` and the path is returned in the result.

IMPORTANT: every terminal command runs sandboxed to the project root. Absolute paths outside the
sandbox are rejected.
"""


def build_system_prompt(
    sandbox_root: Path,
    skill_registry: Optional[SkillRegistry] = None,
    subagent_registry: Optional[SubAgentRegistry] = None,
    active_categories: Optional[List[str]] = None,
) -> str:
    """Build the agent system prompt.

    `active_categories` controls which OPT-IN capability sections are included
    on top of the always-on `terminal` tool. Pass None (or a list containing
    "terminal") to keep the legacy behavior. Note: as of v2.4.0 the only direct
    tool is `terminal`; the router now only governs memory / skills / subagents.
    """
    prompt = f"""You are IndusBuilt, an expert coding assistant.

Sandbox directory: {sandbox_root}

IMPORTANT RULES:
- You can ONLY read, list, or edit files INSIDE the sandbox directory above.
- Never attempt to access files outside of the sandbox.
- Your only direct tool is `terminal` — use shell one-liners for reading,
  searching, and modifying files. There are no dedicated read/edit/grep tools.
- When creating code, follow best practices and write clean, documented code.
- Before modifying a file, peek at it (`cat` or `sed -n`) to understand its shape.
- Chain terminal calls as needed to complete tasks.
- When done, give a clear summary of what you changed.
"""
    prompt += _TERMINAL_AWARE_PARAGRAPH

    active = set(active_categories) if active_categories is not None else set(CATEGORY_ORDER)

    if "subagents" in active:
        prompt += _SUBAGENTS_BEHAVIORAL_PARAGRAPH

    for cat in CATEGORY_ORDER:
        if cat in active and cat in _CATEGORY_SECTION_TEXT:
            prompt += "\n" + _CATEGORY_SECTION_TEXT[cat] + "\n"

    if subagent_registry is not None:
        catalog = subagent_registry.catalog_prompt()
        if catalog:
            prompt += "\n" + catalog + "\n"

    if skill_registry is not None:
        catalog = skill_registry.catalog_prompt()
        active_skills = skill_registry.active_prompt()
        if catalog:
            prompt += (
                "\nactivate_skill – load skill instructions when available skills match the task\n"
                "\nSKILLS:\n"
                + catalog
                + "\n"
            )
        if active_skills:
            prompt += "\n" + active_skills + "\n"

    return prompt


# ── Provider helpers ─────────────────────────────────────────────────────────
def _provider_model_ref(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _provider_env_key_name(provider: str) -> str:
    mapping = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    return mapping.get(provider, "")


def _validate_api_key(provider: str, api_key: str) -> Optional[str]:
    """Return a human-readable validation error, or None when key is usable."""
    if not api_key:
        return "API key is empty."

    if any(ord(ch) > 127 for ch in api_key):
        return (
            f"Saved {provider} API key contains non-ASCII characters. "
            "Re-enter it using /key and paste a plain text key."
        )

    if provider == "openai" and not api_key.startswith("sk-"):
        return "OpenAI key looks invalid (expected to start with 'sk-')."
    if provider == "anthropic" and not api_key.startswith("sk-ant-"):
        return "Anthropic key looks invalid (expected to start with 'sk-ant-')."
    if provider == "gemini" and not (api_key.startswith("AIza") or api_key.startswith("gemini_")):
        return "Gemini key looks unusual. AI Studio keys usually start with 'AIza'."

    return None


def _get_effective_api_key(settings: Dict[str, Any], provider: str) -> str:
    saved = get_api_key(settings, provider)
    if saved:
        return saved
    env_key_name = _provider_env_key_name(provider)
    return os.environ.get(env_key_name, "").strip() if env_key_name else ""


def _format_diff(before: str, after: str, max_lines: int = 60) -> str:
    """Build a tiny unified-style diff for UI display."""
    import difflib
    diff = list(difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
        n=2,
    ))
    if not diff:
        return ""
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... ({len(diff) - max_lines} more lines)"]
    return "\n".join(diff)


# ── Slash command spec (shared with the TUI) ─────────────────────────────────
SLASH_COMMANDS: List[Dict[str, str]] = [
    {"name": "help",       "description": "Show slash command help",         "arg": ""},
    {"name": "key",        "description": "Set API key for a provider",       "arg": "[provider]"},
    {"name": "provider",   "description": "Switch the active provider",       "arg": ""},
    {"name": "model",      "description": "Choose model for the provider",    "arg": ""},
    {"name": "show",       "description": "Show current provider + model",    "arg": ""},
    {"name": "router",     "description": "Router: show/on/off/provider/model", "arg": "<action>"},
    {"name": "memory",     "description": "Memory: status/search/summarize/rebuild", "arg": "<action>"},
    {"name": "skills",     "description": "List or load a skill",             "arg": "[name]"},
    {"name": "subagents",  "description": "List or inspect a subagent",       "arg": "[name]"},
    {"name": "subagent model", "description": "Choose the subagent model",    "arg": ""},
    {"name": "hooks",      "description": "List or inspect a hook",          "arg": "[name]"},
    {"name": "create skill",    "description": "Create a new project skill",  "arg": "[name]"},
    {"name": "create subagent", "description": "Create a new subagent",        "arg": "[name]"},
    {"name": "create hook",     "description": "Create a new hook",           "arg": "[name]"},
    {"name": "exit",       "description": "Exit the agent",                   "arg": ""},
    {"name": "quit",       "description": "Exit the agent",                   "arg": ""},
]


# ── AgentController ──────────────────────────────────────────────────────────
class AgentController:
    """
    Drives the agent loop. Runs in a worker thread. Emits AgentEvent
    instances to its sink; the UI is responsible for rendering them.

    Input flow: UI calls submit_text("hello") to push a user prompt or
    slash command. The controller's worker pops it from the queue and
    processes it.

    Interactive flow: for things like provider selection or key entry,
    the controller emits AskChoice / AskInput. The UI shows a modal and
    resolves the embedded Future by calling submit_choice / submit_text.
    """

    def __init__(
        self,
        sandbox_root: Path,
        settings: Dict[str, Any],
        sink: EventSink,
    ) -> None:
        self.sandbox_root = sandbox_root.resolve()
        self.settings = settings
        self._sink = sink
        self._input_queue: "queue.Queue[str]" = queue.Queue()
        self._pending_ask: Optional[concurrent.futures.Future] = None
        self._pending_token: Optional[str] = None
        self._cancelled = threading.Event()
        self._worker: Optional[threading.Thread] = None

        self.skill_registry = SkillRegistry(self.sandbox_root)
        self.skill_registry.refresh()
        self.skill_commands = SkillCommandHandler(self.skill_registry)

        self.subagent_registry = SubAgentRegistry(self.sandbox_root)
        self.subagent_registry.refresh()
        self.subagent_commands = SubAgentCommandHandler(self.subagent_registry)

        self.hook_registry = HookRegistry(self.sandbox_root)
        self.hook_registry.refresh()
        self.hook_commands = HookCommandHandler(self.hook_registry)

        self.conversation: List[Dict[str, Any]] = []
        self.context_manager = ContextManager(self.sandbox_root)
        self.tool_registry = make_tools(
            self.sandbox_root,
            skill_registry=self.skill_registry,
            context_manager=self.context_manager,
            conversation_ref=self.conversation,
        )

        self.session_id = uuid.uuid4().hex[:12]

    # ── Public API used by the UI ───────────────────────────────────────────
    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._cancelled.set()
        try:
            self._input_queue.put_nowait("__EXIT__")
        except Exception:
            pass

    def submit_text(self, text: str) -> None:
        try:
            self._input_queue.put_nowait(text)
        except Exception:
            pass

    def submit_choice(self, token: str, index: int) -> None:
        if self._pending_ask is not None and self._pending_token == token:
            fut = self._pending_ask
            self._pending_ask = None
            self._pending_token = None
            fut.set_result(index)

    def submit_input(self, token: str, value: str) -> None:
        if self._pending_ask is not None and self._pending_token == token:
            fut = self._pending_ask
            self._pending_ask = None
            self._pending_token = None
            fut.set_result(value)

    def cancel_ask(self, token: str) -> None:
        if self._pending_ask is not None and self._pending_token == token:
            fut = self._pending_ask
            self._pending_ask = None
            self._pending_token = None
            fut.set_result(None)

    # ── Internals ───────────────────────────────────────────────────────────
    def _emit(self, event: AgentEvent) -> None:
        try:
            self._sink(event)
        except Exception:
            pass

    def _build_hook_context(self, **kwargs: Any) -> HookEventContext:
        return HookEventContext(
            session_id=self.session_id,
            cwd=str(self.sandbox_root),
            **kwargs,
        )

    def _ask_choice(self, title: str, options: List[str], hint: str = "Use ↑/↓ and Enter") -> int:
        token = uuid.uuid4().hex
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._pending_ask = fut
        self._pending_token = token
        self._emit(AskChoice(token=token, title=title, options=options, hint=hint, future=fut))
        result = fut.result()
        if result is None:
            return -1
        return int(result)

    def _ask_input(self, title: str, placeholder: str = "", password: bool = False) -> Optional[str]:
        token = uuid.uuid4().hex
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._pending_ask = fut
        self._pending_token = token
        self._emit(AskInput(token=token, title=title, placeholder=placeholder, password=password, future=fut))
        return fut.result()

    def _refresh_runtime_state(self) -> Dict[str, str]:
        provider = get_active_provider(self.settings)
        model = get_model(self.settings, provider)
        subagent_model = get_subagent_model(self.settings, provider)
        api_key = _get_effective_api_key(self.settings, provider)
        router_enabled = get_router_enabled(self.settings)
        router_provider = get_router_provider(self.settings)
        router_model = get_router_model(self.settings, router_provider)
        router_api_key = _get_effective_api_key(self.settings, router_provider) if router_enabled else ""
        return {
            "provider": provider,
            "model": model,
            "subagent_model": subagent_model,
            "api_key": api_key,
            "router_enabled": "1" if router_enabled else "0",
            "router_provider": router_provider,
            "router_model": router_model,
            "router_api_key": router_api_key,
        }

    def _hook_sink(self, hook_result) -> None:
        self._emit(HookFired(
            hook_name=hook_result.hook_name,
            event=hook_result.event,
            decision=hook_result.decision,
            reason=hook_result.reason or "",
            elapsed_s=hook_result.elapsed_s,
            error=hook_result.error,
        ))

    # ── Provider / model selection helpers (event-driven) ───────────────────
    def _select_provider_interactive(self, current_provider: str) -> Optional[str]:
        labels = [
            f"{provider} {'(active)' if provider == current_provider else ''}".rstrip()
            for provider in PROVIDERS
        ]
        idx = self._ask_choice("Select Provider", labels)
        if idx < 0 or idx >= len(PROVIDERS):
            return None
        return PROVIDERS[idx]

    def _select_model_interactive(self, provider: str, current_model: str) -> Optional[str]:
        choices = MODEL_CHOICES.get(provider, [current_model])
        labels = [
            f"{model} {'(current)' if model == current_model else ''}".rstrip()
            for model in choices
        ]
        idx = self._ask_choice(f"Select Model ({provider})", labels)
        if idx < 0 or idx >= len(choices):
            return None
        return choices[idx]

    def _select_subagent_model_interactive(self, provider: str, current_model: str) -> Optional[str]:
        choices = SUBAGENT_MODEL_CHOICES.get(provider, [current_model])
        labels = [
            f"{model} {'(current)' if model == current_model else ''}".rstrip()
            for model in choices
        ]
        idx = self._ask_choice(f"Select SubAgent Model ({provider})", labels)
        if idx < 0 or idx >= len(choices):
            return None
        return choices[idx]

    def _select_router_provider_interactive(self, current_provider: str) -> Optional[str]:
        labels = []
        provider_options: List[Optional[str]] = []
        for provider in PROVIDERS:
            labels.append(
                f"{provider} {'(active)' if provider == current_provider else ''}".rstrip()
            )
            provider_options.append(provider)
        labels.append(f"(follow main provider) {'(active)' if not current_provider else ''}".rstrip())
        provider_options.append(None)
        idx = self._ask_choice("Select Router Provider", labels)
        if idx < 0 or idx >= len(provider_options):
            return None
        return provider_options[idx]  # type: ignore[return-value]

    def _select_router_model_interactive(self, provider: str, current_model: str) -> Optional[str]:
        choices = ROUTER_MODEL_CHOICES.get(provider, [current_model])
        labels = [
            f"{model} {'(current)' if model == current_model else ''}".rstrip()
            for model in choices
        ]
        labels.append(
            f"(follow main model) {'(current)' if not current_model or current_model == get_model(self.settings, provider) else ''}".rstrip()
        )
        idx = self._ask_choice(f"Select Router Model ({provider})", labels)
        if idx < 0 or idx >= len(choices) + 1:
            return None
        if idx == len(choices):
            return ""
        return choices[idx]

    def _set_key_interactive(self, provider: Optional[str] = None) -> None:
        target_provider = provider or self._select_provider_interactive(get_active_provider(self.settings))
        if not target_provider:
            return
        entered = self._ask_input(
            title=f"Enter {target_provider} API key",
            placeholder="paste your key and press Enter",
            password=True,
        )
        if entered is None:
            return
        entered = entered.strip()
        if not entered:
            self._emit(Warning("Empty key. Nothing saved."))
            return
        validation_error = _validate_api_key(target_provider, entered)
        if validation_error:
            self._emit(ErrorEvent(validation_error))
            return
        set_api_key(self.settings, target_provider, entered)
        save_settings(self.settings)
        self._emit(Success(f"Saved key for provider '{target_provider}'."))

    def _ensure_active_provider_key(self) -> bool:
        """Returns True if a usable key is present. Emits events on failure."""
        while not self._cancelled.is_set():
            provider = get_active_provider(self.settings)
            existing = _get_effective_api_key(self.settings, provider)
            if existing:
                validation_error = _validate_api_key(provider, existing)
                if validation_error is None:
                    return True
                self._emit(ErrorEvent(validation_error))
                choice = self._ask_choice(
                    "Provider Key Invalid",
                    [
                        "Re-enter key for current provider",
                        "Switch provider",
                        "Exit",
                    ],
                )
                if choice == 0:
                    self._set_key_interactive(provider=provider)
                    continue
                if choice == 1:
                    selected = self._select_provider_interactive(provider)
                    if selected:
                        set_active_provider(self.settings, selected)
                        save_settings(self.settings)
                    continue
                return False

            self._emit(ErrorEvent(f"No API key saved for provider '{provider}'."))
            choice = self._ask_choice(
                "Provider Key Required",
                [
                    "Add key for current provider",
                    "Switch provider",
                    "Exit",
                ],
            )
            if choice == 0:
                self._set_key_interactive(provider=provider)
            elif choice == 1:
                selected = self._select_provider_interactive(provider)
                if selected:
                    set_active_provider(self.settings, selected)
                    save_settings(self.settings)
            else:
                return False
        return False

    # ── Slash command handling ──────────────────────────────────────────────
    def _handle_slash_command(self, command: str) -> None:
        raw_command = command.strip()
        raw = raw_command.lower()

        if raw in ("/", "/menu"):
            self._command_palette()
            return

        if raw.startswith("/key"):
            self._set_key_interactive()
            return

        if raw.startswith("/provider"):
            provider_before = get_active_provider(self.settings)
            selected_provider = self._select_provider_interactive(provider_before)
            if selected_provider:
                set_active_provider(self.settings, selected_provider)
                save_settings(self.settings)
                self._emit(Success(f"Active provider set to {selected_provider}."))
            return

        if raw.startswith("/model"):
            provider = get_active_provider(self.settings)
            current_model = get_model(self.settings, provider)
            selected_model = self._select_model_interactive(provider, current_model)
            if selected_model:
                set_model(self.settings, provider, selected_model)
                save_settings(self.settings)
                self._emit(Success(f"Model for {provider} set to {selected_model}."))
            return

        if raw.startswith("/show"):
            state = self._refresh_runtime_state()
            router_status = (
                f"enabled ({state['router_provider']}/{state['router_model']})"
                if state["router_enabled"] == "1"
                else "disabled"
            )
            self._emit(Info(
                f"Provider: {state['provider']}  |  Model: {state['model']}  |  "
                f"SubAgent Model: {state['subagent_model']}  |  "
                f"Router: {router_status}"
            ))
            return

        if raw.startswith("/skills"):
            self._skills_flow(raw_command)
            return

        if raw.startswith("/subagent model"):
            provider = get_active_provider(self.settings)
            current_sa_model = get_subagent_model(self.settings, provider)
            selected_sa_model = self._select_subagent_model_interactive(provider, current_sa_model)
            if selected_sa_model:
                set_subagent_model(self.settings, provider, selected_sa_model)
                save_settings(self.settings)
                self._emit(Success(f"SubAgent model for {provider} set to {selected_sa_model}."))
            return

        if raw.startswith("/router"):
            self._router_flow(raw_command)
            return

        if raw.startswith("/subagents"):
            self._subagents_flow(raw_command)
            return

        if raw.startswith("/memory"):
            self._memory_flow(raw_command)
            return

        if raw.startswith("/create skill"):
            self._create_skill_flow(raw_command)
            return

        if raw.startswith("/create subagent"):
            self._create_subagent_flow(raw_command)
            return

        if raw.startswith("/hooks"):
            self._hooks_flow(raw_command)
            return

        if raw.startswith("/create hook"):
            self._create_hook_flow(raw_command)
            return

        if raw.startswith("/help"):
            self._emit_help()
            return

        if raw.startswith("/exit") or raw.startswith("/quit"):
            self._emit(SessionEnd(reason="slash_exit"))
            self._cancelled.set()
            try:
                self._input_queue.put_nowait("__EXIT__")
            except Exception:
                pass
            return

        self._emit(Warning(f"Unknown command: {raw_command}"))
        self._emit_help()

    def _emit_help(self) -> None:
        lines = ["Slash commands:"]
        for cmd in SLASH_COMMANDS:
            arg = f" {cmd['arg']}" if cmd["arg"] else ""
            lines.append(f"  /{cmd['name']}{arg}  — {cmd['description']}")
        self._emit(Info("\n".join(lines)))

    def _command_palette(self) -> None:
        options = [f"/{c['name']}  — {c['description']}" for c in SLASH_COMMANDS[:-2]]
        options.append("Cancel")
        idx = self._ask_choice("Command Palette", options)
        if idx < 0 or idx >= len(SLASH_COMMANDS) - 2:
            return
        # Open a sub-prompt that re-enters slash handling.
        # We do this by emitting a hint and letting the user re-type.
        self._emit(Info(f"Type /{SLASH_COMMANDS[idx]['name']} and press Enter to run it."))

    def _skills_flow(self, command: str) -> None:
        parts = command.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            self.skill_registry.refresh()
            skills = self.skill_registry.list_skills()
            if not skills:
                self._emit(Warning("No skills found. Create one with /create skill."))
                return
            options = [f"{s.name}  —  {s.description}" for s in skills]
            options.append("Refresh skills")
            options.append("Cancel")
            idx = self._ask_choice("Available Skills", options)
            if idx < 0 or idx >= len(options) - 1:
                return
            if idx < len(skills):
                self._activate_skill(skills[idx].name)
            else:
                self.skill_registry.refresh()
                self._emit(Success("Skills refreshed."))
            return
        self._activate_skill(arg)

    def _activate_skill(self, name: str) -> None:
        from .skills import normalize_skill_name
        self.skill_registry.refresh()
        skill_name = name if name in self.skill_registry.skills else normalize_skill_name(name)
        result = self.skill_registry.activate(skill_name)
        if "error" in result:
            self._emit(ErrorEvent(result["error"]))
            return
        if result.get("already_active"):
            self._emit(Success(f"Skill '{skill_name}' is already loaded."))
        else:
            self._emit(Success(
                f"Loaded skill '{skill_name}' into the agent context "
                f"({result.get('resource_count', 0)} resources)."
            ))

    def _subagents_flow(self, command: str) -> None:
        parts = command.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        self.subagent_registry.refresh()
        agents = self.subagent_registry.list_agents()
        if not agents:
            self._emit(Warning("No subagents found."))
            return
        if not arg:
            options = [f"{a.name}  —  {a.description}" for a in agents]
            options.append("Cancel")
            idx = self._ask_choice("Available SubAgents", options)
            if idx < 0 or idx >= len(agents):
                return
            sa = agents[idx]
        else:
            from .skills import normalize_skill_name
            target = arg if arg in [a.name for a in agents] else normalize_skill_name(arg)
            sa = self.subagent_registry.get(target)
            if not sa:
                self._emit(ErrorEvent(f"Unknown subagent: '{arg}'"))
                return

        self._emit(Info(
            f"SubAgent: {sa.name}\n"
            f"  Description : {sa.description}\n"
            f"  Tools       : {', '.join(sa.tools)}\n"
            f"  Max turns   : {sa.max_turns}\n"
            f"  Source      : {'builtin' if sa.source == 'builtin' else sa.source}"
        ))

    def _memory_flow(self, command: str) -> None:
        parts = command.strip().split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action in ("status", "show"):
            self._emit(MemoryStatus(status=self.context_manager.status()))
            return
        if action == "search":
            query = parts[2].strip() if len(parts) > 2 else ""
            if not query:
                self._emit(ErrorEvent("Usage: /memory search <query>"))
                return
            self._emit(Info(json.dumps(
                self.context_manager.search_memory(query=query), indent=2, ensure_ascii=False
            )))
            return
        if action == "summarize":
            result = self.context_manager.summarize_session(self.conversation, reason="manual")
            self._emit(Success(f"Session summarized to {result['path']}"))
            return
        if action == "rebuild":
            result = self.context_manager.rebuild_index()
            self._emit(Success(f"Memory index rebuilt with {result['count']} entries."))
            return
        self._emit(ErrorEvent("Unknown /memory command. Use: status, search, summarize, rebuild"))

    def _create_skill_flow(self, command: str) -> None:
        from .skills import normalize_skill_name
        lowered = command.lower()
        prefix = "/create skill"
        name = command[len(prefix):].strip() if lowered.startswith(prefix) else ""
        if not name:
            name = self._ask_input("Skill name", "e.g. python-tests")
            if not name:
                return
        description = self._ask_input("Skill description", "When should the agent load this skill?")
        if not description:
            return
        slug = normalize_skill_name(name)
        result = self.skill_registry.create_skill(slug, description)
        if "error" in result:
            self._emit(ErrorEvent(result["error"]))
            return
        self._emit(Success(f"Created skill '{result['name']}' at {result['path']}."))

    def _create_subagent_flow(self, command: str) -> None:
        parts = command.strip().split()
        name_from_cmd = ""
        for i, part in enumerate(parts):
            if part.lower() == "subagent" and i + 1 < len(parts):
                name_from_cmd = parts[i + 1]
                break
        name = name_from_cmd or self._ask_input("SubAgent name", "e.g. reviewer")
        if not name:
            return
        description = self._ask_input("SubAgent description", "When should the main agent use this subagent?")
        if not description:
            return
        purpose = self._ask_input("SubAgent purpose", "What does this subagent do?")
        if not purpose:
            return
        result = self.subagent_registry.create_custom(name, description, purpose)
        if "error" in result:
            self._emit(ErrorEvent(result["error"]))
            return
        self._emit(Success(
            f"Created subagent '{result['name']}' at {result['path']}.\n"
            f"Edit the AGENT.md file to customize its instructions and tool access."
        ))

    def _hooks_flow(self, command: str) -> None:
        from .hooks import HookEvent
        parts = command.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        self.hook_registry.refresh()
        hooks = self.hook_registry.list_hooks()
        if not arg:
            if not hooks:
                self._emit(Warning("No hooks configured. Create one with /create hook."))
                return
            options = [f"[{h.event}] {h.name}  ({h.hook_type})" for h in hooks]
            options.append("Cancel")
            idx = self._ask_choice("Active Hooks", options)
            if idx < 0 or idx >= len(hooks):
                return
            hook = hooks[idx]
        else:
            hook = next((h for h in hooks if h.name == arg), None)
            if not hook:
                self._emit(ErrorEvent(f"Hook not found: {arg}"))
                return

        matcher = f"  (matches: {hook.matcher})" if hook.matcher else ""
        self._emit(Info(
            f"Hook: {hook.name}\n"
            f"  Event:      {hook.event}\n"
            f"  Type:       {hook.hook_type}{matcher}\n"
            f"  Timeout:    {hook.timeout}s\n"
            f"  Deny mode:  {hook.decision_on_deny}\n"
            f"  Source:     {hook.source}"
        ))

    def _create_hook_flow(self, command: str) -> None:
        from .hooks import HookEvent, HookType
        lowered = command.lower()
        prefix = "/create hook"
        name = command[len(prefix):].strip() if lowered.startswith(prefix) else ""
        if not name:
            name = self._ask_input("Hook name", "e.g. block-secret-writes")
            if not name:
                return
        events = [e.value for e in HookEvent]
        evt_idx = self._ask_choice("Hook event", events)
        if evt_idx < 0:
            return
        event = events[evt_idx]

        types = [t.value for t in HookType]
        type_idx = self._ask_choice("Hook type", types)
        if type_idx < 0:
            return
        hook_type = types[type_idx]

        matcher = self._ask_input("Matcher regex (leave empty for all tools)", "e.g. ^terminal$") or None
        command_str = None
        prompt_str = None
        model_str = None

        if hook_type == "command":
            command_str = self._ask_input("Shell command", "echo '{}'")
            if not command_str:
                return
        elif hook_type in ("prompt", "agent"):
            prompt_str = self._ask_input("Prompt (use $ARGUMENTS for context)", "Review and decide")
            if not prompt_str:
                return
            model_str = self._ask_input("Model (leave empty for default)") or None

        timeout_input = self._ask_input("Timeout in seconds", "30") or "30"
        try:
            timeout = int(timeout_input)
        except ValueError:
            timeout = 30

        deny = self._ask_input("On deny: block | warn | ask", "block") or "block"
        if deny not in ("block", "warn", "ask"):
            deny = "block"

        result = self.hook_registry.create_hook(
            name=name,
            event=event,
            hook_type=hook_type,
            matcher=matcher,
            command=command_str,
            prompt=prompt_str,
            model=model_str,
            timeout=timeout,
            decision_on_deny=deny,
        )
        if "error" in result:
            self._emit(ErrorEvent(result["error"]))
            return
        self._emit(Success(f"Created hook '{result['name']}' at {result['path']}"))

    # ── Router flow ──────────────────────────────────────────────────────────
    def _router_flow(self, command: str) -> None:
        """Handle the /router slash command.

        Sub-commands: show | on | off | provider | model
        """
        parts = command.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else "show"
        action = arg.lower().split()[0] if arg else "show"

        state = self._refresh_runtime_state()

        if action in ("show", "status"):
            enabled = state["router_enabled"] == "1"
            provider = state["router_provider"]
            model = state["router_model"]
            api_key_present = bool(state["router_api_key"])
            status_text = "enabled" if enabled else "disabled"
            provider_text = provider if provider else f"(follow main → {get_active_provider(self.settings)})"
            model_text = model if model else f"(follow main → {state['model']})"
            self._emit(Info(
                f"Router: {status_text}\n"
                f"  Provider: {provider_text}\n"
                f"  Model:    {model_text}\n"
                f"  API key:  {'present' if api_key_present else 'MISSING'}\n"
                f"  Tip: set a cheap/fast model (e.g. gpt-4o-mini, claude-haiku, gemini-flash-lite)"
                f" to keep routing costs near zero."
            ))
            return

        if action == "on":
            set_router_enabled(self.settings, True)
            save_settings(self.settings)
            self._emit(Success("Router enabled. Tool categories will be selected per user turn."))
            return

        if action == "off":
            set_router_enabled(self.settings, False)
            save_settings(self.settings)
            self._emit(Success("Router disabled. Main agent now sees all tools on every turn."))
            return

        if action == "provider":
            current_provider = get_router_provider(self.settings)
            selected = self._select_router_provider_interactive(current_provider)
            if selected is None:
                return
            set_router_provider(self.settings, selected)
            save_settings(self.settings)
            resolved = selected or "(follow main)"
            self._emit(Success(f"Router provider set to {resolved}."))
            return

        if action == "model":
            current_provider = get_router_provider(self.settings)
            current_model = get_router_model(self.settings, current_provider)
            selected_model = self._select_router_model_interactive(current_provider, current_model)
            if selected_model is None:
                return
            set_router_model(self.settings, current_provider, selected_model)
            save_settings(self.settings)
            resolved = selected_model or "(follow main model)"
            self._emit(Success(f"Router model for {current_provider} set to {resolved}."))
            return

        self._emit(Warning(
            f"Unknown /router action: '{action}'. Use: show | on | off | provider | model"
        ))

    # ── Router core ─────────────────────────────────────────────────────────
    def _route_tools(
        self,
        user_message: str,
        state: Dict[str, str],
        rerouted: bool = False,
        requested_missing: Optional[List[str]] = None,
    ) -> Tuple[Optional[set], List[str], str, str]:
        """Decide which tool categories the main agent needs for `user_message`.

        Returns a tuple of:
            (allowed_tool_names_or_None, categories, router_provider, router_model)

        `allowed_tool_names_or_None` is None when the router is disabled or
        something went wrong — meaning the main agent should see ALL tools.

        Emits a RouterDecision event so the UI can surface what was chosen.
        """
        if state["router_enabled"] != "1":
            return None, [], "", ""

        router_provider = state["router_provider"]
        router_model = state["router_model"]
        router_api_key = state["router_api_key"]

        if not router_api_key:
            self._emit(Warning(
                f"Router enabled but no API key for provider '{router_provider}'. "
                f"Falling back to all tools. Use /router show to inspect."
            ))
            self._emit(RouterDecision(
                user_message=user_message,
                categories=[],
                tool_names=[],
                provider=router_provider,
                model=router_model,
                error="missing_api_key",
                rerouted=rerouted,
            ))
            return None, [], router_provider, router_model

        validation_error = _validate_api_key(router_provider, router_api_key)
        if validation_error:
            self._emit(Warning(
                f"Router API key for '{router_provider}' looks invalid: {validation_error}. "
                f"Falling back to all tools."
            ))
            self._emit(RouterDecision(
                user_message=user_message,
                categories=[],
                tool_names=[],
                provider=router_provider,
                model=router_model,
                error=validation_error,
                rerouted=rerouted,
            ))
            return None, [], router_provider, router_model

        # Build the system prompt describing categories.
        category_lines = "\n".join(
            f"- {cat}: {CATEGORY_DESCRIPTIONS[cat]}" for cat in CATEGORY_ORDER
        )
        router_system = ROUTER_SYSTEM_PROMPT.format(categories=category_lines)

        # Build the user message for the router. On re-route, hint that some
        # tools were already requested and the main agent needs access to them.
        router_user = f"User message:\n{user_message}"
        if rerouted and requested_missing:
            missing_str = ", ".join(requested_missing)
            router_user += (
                f"\n\nNote: the main agent already tried to call these tools "
                f"and they were NOT in the previously-granted set: {missing_str}. "
                f"Expand the categories so these tools are included."
            )

        start = time.time()
        try:
            response = completion(
                model=_provider_model_ref(router_provider, router_model),
                messages=[
                    {"role": "system", "content": router_system},
                    {"role": "user", "content": router_user},
                ],
                api_key=router_api_key,
            )
        except Exception as exc:
            elapsed = time.time() - start
            self._emit(Warning(
                f"Router call to {router_provider}/{router_model} failed: {exc}. "
                f"Falling back to all tools."
            ))
            self._emit(RouterDecision(
                user_message=user_message,
                categories=[],
                tool_names=[],
                provider=router_provider,
                model=router_model,
                elapsed_s=elapsed,
                error=str(exc),
                rerouted=rerouted,
            ))
            return None, [], router_provider, router_model

        elapsed = time.time() - start
        text: str = ""
        try:
            text = (response.choices[0].message.content or "") if getattr(response, "choices", None) else ""
        except Exception:
            text = ""

        categories = parse_router_response(text)
        # Sanitize: keep only known categories, in canonical order, deduped.
        seen: set = set()
        clean_categories: List[str] = []
        for cat in CATEGORY_ORDER:
            if cat in categories and cat not in seen:
                seen.add(cat)
                clean_categories.append(cat)
        # Anything the router returned that wasn't a known category is dropped.

        tool_names = categories_to_tool_names(clean_categories)
        allowed_set = set(tool_names) if clean_categories else set()

        # If on re-route we learned about missing tools, make sure they're covered.
        if rerouted and requested_missing:
            missing_set = set(requested_missing)
            if not missing_set.issubset(allowed_set):
                # Add the categories that contain the missing tools.
                for tool_name in missing_set:
                    for cat, tools in TOOL_CATEGORIES.items():
                        if tool_name in tools and cat not in clean_categories:
                            clean_categories.append(cat)
                            allowed_set.update(tools)
                # Re-sort categories in canonical order for stable output.
                clean_categories = [c for c in CATEGORY_ORDER if c in clean_categories]
                tool_names = categories_to_tool_names(clean_categories)
                allowed_set = set(tool_names)

        self._emit(RouterDecision(
            user_message=user_message,
            categories=list(clean_categories),
            tool_names=list(tool_names),
            provider=router_provider,
            model=router_model,
            elapsed_s=elapsed,
            rerouted=rerouted,
        ))

        return (allowed_set if allowed_set else None), list(clean_categories), router_provider, router_model

    # ── Streaming ───────────────────────────────────────────────────────────
    def _stream_model_turn(
        self,
        allowed_tools: Optional[set] = None,
        active_categories: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        state = self._refresh_runtime_state()
        validation_error = _validate_api_key(state["provider"], state["api_key"])
        if validation_error:
            raise ValueError(validation_error)

        all_tools = build_openai_tools(
            self.skill_registry,
            context_manager=self.context_manager,
            subagent_registry=self.subagent_registry,
        )
        tools = filter_tools(all_tools, allowed_tools)

        system_prompt = build_system_prompt(
            self.sandbox_root,
            self.skill_registry,
            self.subagent_registry,
            active_categories=active_categories,
        )

        self._emit(Thinking(label="thinking"))
        streamed_text_parts: List[str] = []
        pending_tool_calls: Dict[int, Dict[str, Any]] = {}
        assistant_started = False

        try:
            stream = completion(
                model=_provider_model_ref(state["provider"], state["model"]),
                messages=self.context_manager.build_messages(
                    system_prompt=system_prompt,
                    conversation=self.conversation,
                ),
                tools=tools,
                stream=True,
                api_key=state["api_key"],
            )

            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = choices[0].delta

                if delta.content:
                    if not assistant_started:
                        self._emit(AssistantStart(model=state["model"]))
                        assistant_started = True
                    self._emit(AssistantToken(token=delta.content))
                    streamed_text_parts.append(delta.content)

                if delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        idx = tool_call.index
                        if idx is None:
                            continue
                        if idx not in pending_tool_calls:
                            pending_tool_calls[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tool_call.id:
                            pending_tool_calls[idx]["id"] = tool_call.id
                        if tool_call.function and tool_call.function.name:
                            pending_tool_calls[idx]["function"]["name"] = tool_call.function.name
                        if tool_call.function and tool_call.function.arguments:
                            pending_tool_calls[idx]["function"]["arguments"] += tool_call.function.arguments
        finally:
            self._emit(ThinkingEnd())

        if assistant_started:
            self._emit(AssistantEnd(full_text="".join(streamed_text_parts)))

        ordered_calls = [pending_tool_calls[i] for i in sorted(pending_tool_calls.keys())]
        return {
            "content": "".join(streamed_text_parts),
            "tool_calls": ordered_calls,
        }

    # ── Tool execution ──────────────────────────────────────────────────────
    def _execute_tool_calls(
        self,
        tool_calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        tc_results: Dict[str, Any] = {}

        regular_tcs = [
            tc for tc in tool_calls
            if tc.get("function", {}).get("name") != "call_subagent"
        ]
        subagent_tcs = [
            tc for tc in tool_calls
            if tc.get("function", {}).get("name") == "call_subagent"
        ]

        for tc in regular_tcs:
            tool_name = tc.get("function", {}).get("name", "")
            raw_arguments = tc.get("function", {}).get("arguments", "{}")
            try:
                args = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            call_id = tc.get("id", "")

            pre_tool_hook = self.hook_registry.execute_hooks(
                HookEvent.PreToolUse,
                self._build_hook_context(
                    hook_event_name=HookEvent.PreToolUse,
                    tool_name=tool_name,
                    tool_input=args,
                ),
                settings=self.settings,
                tool_registry=self.tool_registry,
                ui_callback=self._hook_sink,
            )

            if pre_tool_hook.get("modified_input"):
                args = pre_tool_hook["modified_input"]

            self._emit(ToolStart(call_id=call_id, tool_name=tool_name, args=args))
            self._emit(Thinking(label=f"running {tool_name}"))
            start = time.time()

            try:
                if pre_tool_hook.get("decision") == "block":
                    result = {
                        "error": f"Hook blocked tool '{tool_name}': "
                                 f"{self._hook_block_reason(pre_tool_hook)}"
                    }
                elif pre_tool_hook.get("decision") == "ask":
                    choice = self._ask_choice(
                        f"Hook requires approval for '{tool_name}'",
                        ["Approve", "Deny"],
                    )
                    if choice == 0:
                        fn = self.tool_registry.get(tool_name)
                        result = fn(**args) if fn else {"error": f"Unknown tool: {tool_name}"}
                    else:
                        result = {"error": f"User denied tool '{tool_name}' via hook"}
                else:
                    fn = self.tool_registry.get(tool_name)
                    if pre_tool_hook.get("decision") == "warn" and pre_tool_hook.get("system_messages"):
                        for msg in pre_tool_hook["system_messages"]:
                            self._emit(Warning(str(msg)))
                    if fn:
                        result = fn(**args)
                    else:
                        result = {"error": f"Unknown tool: {tool_name}"}
            except Exception as exc:
                result = {"error": str(exc)}
            finally:
                self._emit(ThinkingEnd())

            elapsed = time.time() - start
            self._emit(ToolEnd(
                call_id=call_id,
                tool_name=tool_name,
                args=args,
                result=result,
                elapsed_s=elapsed,
            ))

            if "error" in result:
                self.hook_registry.execute_hooks(
                    HookEvent.PostToolUseFailure,
                    self._build_hook_context(
                        hook_event_name=HookEvent.PostToolUseFailure,
                        tool_name=tool_name,
                        tool_input=args,
                        tool_output=result,
                        error_message=result.get("error", ""),
                    ),
                    settings=self.settings,
                    tool_registry=self.tool_registry,
                    ui_callback=self._hook_sink,
                )
            else:
                post_hook = self.hook_registry.execute_hooks(
                    HookEvent.PostToolUse,
                    self._build_hook_context(
                        hook_event_name=HookEvent.PostToolUse,
                        tool_name=tool_name,
                        tool_input=args,
                        tool_output=result,
                    ),
                    settings=self.settings,
                    tool_registry=self.tool_registry,
                    ui_callback=self._hook_sink,
                )
                if post_hook.get("additional_context") and isinstance(result, dict):
                    result["_hook_context"] = post_hook["additional_context"]

            result = self.context_manager.maybe_offload_tool_result(tool_name, result)
            self.context_manager.register_tool_result(tool_name, result)
            tc_results[call_id] = result

        if subagent_tcs:
            tc_results.update(self._execute_subagent_calls(subagent_tcs))

        return tc_results

    def _hook_block_reason(self, pre_tool_hook: Dict[str, Any]) -> str:
        results = pre_tool_hook.get("results") or []
        if results and getattr(results[0], "reason", None):
            return results[0].reason
        return "blocked by hook"

    def _execute_subagent_calls(self, subagent_tcs: List[Dict[str, Any]]) -> Dict[str, Any]:
        state = self._refresh_runtime_state()
        tc_results: Dict[str, Any] = {}
        valid_calls: List[Any] = []

        for tc in subagent_tcs:
            raw_args = tc.get("function", {}).get("arguments", "{}")
            try:
                sa_args = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                sa_args = {}
            sa_name = sa_args.get("subagent", "")
            task = sa_args.get("task", "")
            tc_id = tc.get("id", "")
            sa_def = self.subagent_registry.get(sa_name)
            if sa_def:
                valid_calls.append((sa_def, task, tc_id))
            else:
                available = [a.name for a in self.subagent_registry.list_agents()]
                tc_results[tc_id] = {"error": f"Unknown subagent: '{sa_name}'", "available": available}

        if not valid_calls:
            return tc_results

        dispatch_calls = [{"name": d.name, "task": t} for d, t, _ in valid_calls]
        self._emit(SubagentDispatch(calls=dispatch_calls))

        for sa_def, sa_task, _ in valid_calls:
            self.hook_registry.execute_hooks(
                HookEvent.SubagentStart,
                self._build_hook_context(
                    hook_event_name=HookEvent.SubagentStart,
                    subagent_name=sa_def.name,
                    subagent_task=sa_task,
                ),
                settings=self.settings,
                tool_registry=self.tool_registry,
                ui_callback=self._hook_sink,
            )

        plural = "s" if len(valid_calls) > 1 else ""
        self._emit(Thinking(label=f"running {len(valid_calls)} subagent{plural} in parallel"))
        try:
            sa_results = run_subagents_parallel(
                [(d, t) for d, t, _ in valid_calls],
                self.tool_registry,
                build_openai_tools(self.skill_registry, context_manager=self.context_manager),
                state["provider"],
                state["subagent_model"],
                state["api_key"],
            )
        finally:
            self._emit(ThinkingEnd())

        for (defn, task, tc_id), sa_result in zip(valid_calls, sa_results):
            self._emit(SubagentEnd(
                name=sa_result.name,
                output=sa_result.output,
                elapsed_s=sa_result.elapsed_s,
                turns=sa_result.turns,
                error=sa_result.error,
                task=task,
            ))

            result_dict: Dict[str, Any] = {
                "subagent": sa_result.name,
                "output": sa_result.output,
                "elapsed_s": round(sa_result.elapsed_s, 2),
                "turns": sa_result.turns,
            }
            if sa_result.error:
                result_dict["error"] = sa_result.error
            tc_results[tc_id] = result_dict

            self.hook_registry.execute_hooks(
                HookEvent.SubagentStop,
                self._build_hook_context(
                    hook_event_name=HookEvent.SubagentStop,
                    subagent_name=sa_result.name,
                    subagent_task=task,
                    subagent_result={
                        "output": sa_result.output,
                        "elapsed_s": sa_result.elapsed_s,
                        "turns": sa_result.turns,
                        "error": sa_result.error,
                    },
                ),
                settings=self.settings,
                tool_registry=self.tool_registry,
                ui_callback=self._hook_sink,
            )

        return tc_results

    # ── Main worker loop ────────────────────────────────────────────────────
    def _run(self) -> None:
        state = self._refresh_runtime_state()
        self._emit(SessionStart(
            provider=state["provider"],
            model=state["model"],
            subagent_model=state["subagent_model"],
            sandbox=str(self.sandbox_root),
            skills=[s.name for s in self.skill_registry.list_skills()],
            subagents=[a.name for a in self.subagent_registry.list_agents()],
            hooks=[h.name for h in self.hook_registry.list_hooks()],
            router_enabled=state["router_enabled"] == "1",
            router_provider=state["router_provider"],
            router_model=state["router_model"],
        ))

        # Session start hooks
        self.hook_registry.execute_hooks(
            HookEvent.SessionStart,
            self._build_hook_context(hook_event_name=HookEvent.SessionStart),
            settings=self.settings,
            tool_registry=self.tool_registry,
            ui_callback=self._hook_sink,
        )

        while not self._cancelled.is_set():
            try:
                user_input = self._input_queue.get()
            except Exception:
                continue
            if user_input == "__EXIT__":
                break

            stripped = user_input.strip()
            if not stripped:
                continue

            if stripped.lower() in ("exit", "quit", "bye"):
                self.hook_registry.execute_hooks(
                    HookEvent.SessionEnd,
                    self._build_hook_context(hook_event_name=HookEvent.SessionEnd),
                    settings=self.settings,
                    tool_registry=self.tool_registry,
                    ui_callback=self._hook_sink,
                )
                self._emit(SessionEnd(reason="bye"))
                self._cancelled.set()
                break

            if stripped.startswith("/"):
                try:
                    self._handle_slash_command(stripped)
                except Exception as cmd_err:
                    self._emit(ErrorEvent(f"Command error: {cmd_err}"))
                self._emit(SlashHandled())
                continue

            if not self._ensure_active_provider_key():
                self._emit(Warning("Aborted: no usable API key."))
                continue

            self._emit(UserEcho(text=stripped))
            self.conversation.append({"role": "user", "content": stripped})
            self.context_manager.register_user_turn(stripped)

            user_prompt_hook = self.hook_registry.execute_hooks(
                HookEvent.UserPromptSubmit,
                self._build_hook_context(
                    hook_event_name=HookEvent.UserPromptSubmit,
                    user_prompt=stripped,
                ),
                settings=self.settings,
                tool_registry=self.tool_registry,
                ui_callback=self._hook_sink,
            )
            if user_prompt_hook.get("additional_context"):
                self.conversation.append({
                    "role": "user",
                    "content": f"[Hook context] {user_prompt_hook['additional_context']}",
                })

            # Initial tool routing for this user turn
            turn_state = self._refresh_runtime_state()
            allowed_tools, active_categories, _r_provider, _r_model = self._route_tools(
                stripped, turn_state, rerouted=False
            )
            reroute_count = 0
            max_reroutes = 2

            # Inner agentic loop
            while True:
                if self._cancelled.is_set():
                    break
                try:
                    turn = self._stream_model_turn(
                        allowed_tools=allowed_tools,
                        active_categories=active_categories,
                    )
                except Exception as e:
                    self._emit(ErrorEvent(f"Provider API error: {e}"))
                    break

                assistant_text = turn.get("content", "") or ""
                tool_calls = turn.get("tool_calls", []) or []

                if not tool_calls:
                    if not assistant_text:
                        self._emit(AssistantStart(model=self._refresh_runtime_state()["model"]))
                        self._emit(AssistantEnd(full_text="(empty response)"))
                    else:
                        self.conversation.append({"role": "assistant", "content": assistant_text})
                    self.context_manager.maybe_auto_summarize(self.conversation)
                    self.hook_registry.execute_hooks(
                        HookEvent.Stop,
                        self._build_hook_context(hook_event_name=HookEvent.Stop),
                        settings=self.settings,
                        tool_registry=self.tool_registry,
                        ui_callback=self._hook_sink,
                    )
                    break

                # If the router is filtering tools and the model requested a tool
                # that wasn't granted, re-route (up to max_reroutes times) so the
                # next turn has the right tool set. Without this, the user would
                # have to re-prompt to unlock additional categories.
                if (
                    allowed_tools is not None
                    and reroute_count < max_reroutes
                ):
                    requested = {
                        tc.get("function", {}).get("name", "")
                        for tc in tool_calls
                    }
                    missing = sorted(requested - allowed_tools)
                    if missing:
                        reroute_count += 1
                        self._emit(Info(
                            f"Re-routing to grant: {', '.join(missing)}"
                        ))
                        new_state = self._refresh_runtime_state()
                        allowed_tools, active_categories, _rp, _rm = self._route_tools(
                            stripped, new_state, rerouted=True, requested_missing=missing
                        )
                        # Drop the tool-call turn and re-run with the expanded set.
                        # We do NOT add anything to the conversation here, so the
                        # model's response is silently replaced.
                        continue

                self.conversation.append({
                    "role": "assistant",
                    "content": assistant_text or None,
                    "tool_calls": tool_calls,
                })

                tc_results = self._execute_tool_calls(tool_calls)

                self.hook_registry.execute_hooks(
                    HookEvent.Stop,
                    self._build_hook_context(hook_event_name=HookEvent.Stop),
                    settings=self.settings,
                    tool_registry=self.tool_registry,
                    ui_callback=self._hook_sink,
                )

                for tc in tool_calls:
                    tc_id = tc.get("id", "")
                    result = tc_results.get(tc_id, {"error": "Result not collected"})
                    self.conversation.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                self.context_manager.maybe_auto_summarize(self.conversation)

        # Worker exiting
        self._emit(SessionEnd(reason="worker_exit"))


# ── Backwards-compat shim for the CLI ───────────────────────────────────────
def run_agent(sandbox_root: Path, settings: Dict[str, Any], sink: EventSink) -> AgentController:
    """Create a controller and start its worker thread. The CLI hands the
    returned controller to the Textual app and awaits completion."""
    controller = AgentController(sandbox_root=sandbox_root, settings=settings, sink=sink)
    controller.start()
    return controller
