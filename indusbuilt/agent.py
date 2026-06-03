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
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from litellm import completion

from .context_manager import ContextManager
from .events import (
    AgentEvent,
    AskChoice,
    AskInput,
    AssistantEnd,
    AssistantStart,
    AssistantToken,
    CodeDiff,
    Error as ErrorEvent,
    EventSink,
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
from .hook_commands import HookCommandHandler
from .hooks import HookEvent, HookEventContext, HookRegistry
from .settings import (
    MODEL_CHOICES,
    PROVIDERS,
    SUBAGENT_MODEL_CHOICES,
    get_active_provider,
    get_api_key,
    get_model,
    get_subagent_model,
    save_settings,
    set_active_provider,
    set_api_key,
    set_model,
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
    """Returns the core tools bound to a sandbox_root."""

    def read_file_tool(filename: str, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        try:
            full_path = resolve_sandboxed_path(filename, sandbox_root)
            lines = full_path.read_text(encoding="utf-8").splitlines()
            total = len(lines)
            start = max(0, offset)
            end = min(total, start + max(1, limit))
            chunk = "\n".join(lines[start:end])
            result: Dict[str, Any] = {
                "file_path": str(full_path),
                "content": chunk,
                "lines_returned": end - start,
                "total_lines": total,
                "offset": start,
            }
            if end < total:
                result["has_more"] = True
                result["next_offset"] = end
            return result
        except ValueError as e:
            return {"error": str(e)}
        except FileNotFoundError:
            return {"error": f"File not found: {filename}"}
        except Exception as e:
            return {"error": str(e)}

    def read_files_tool(filenames: List[str], limit: int = 50) -> Dict[str, Any]:
        results = {}
        for filename in filenames:
            try:
                full_path = resolve_sandboxed_path(filename, sandbox_root)
                lines = full_path.read_text(encoding="utf-8").splitlines()
                total = len(lines)
                chunk = "\n".join(lines[:limit])
                entry: Dict[str, Any] = {
                    "content": chunk,
                    "total_lines": total,
                    "lines_returned": min(limit, total),
                }
                if total > limit:
                    entry["has_more"] = True
                    entry["next_offset"] = limit
                results[filename] = entry
            except ValueError as e:
                results[filename] = {"error": str(e)}
            except FileNotFoundError:
                results[filename] = {"error": f"File not found: {filename}"}
            except Exception as e:
                results[filename] = {"error": str(e)}
        return {"files": results, "count": len(filenames)}

    def list_files_tool(path: str = ".") -> Dict[str, Any]:
        try:
            full_path = resolve_sandboxed_path(path, sandbox_root)
            if not full_path.is_dir():
                return {"error": f"Not a directory: {path}"}
            all_files = []
            for item in sorted(full_path.iterdir()):
                all_files.append({
                    "filename": item.name,
                    "type": "file" if item.is_file() else "dir"
                })
            return {"path": str(full_path), "files": all_files}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    def edit_file_tool(
        path: str,
        new_str: str = "",
        old_str: str = "",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            full_path = resolve_sandboxed_path(path, sandbox_root)
            full_path.parent.mkdir(parents=True, exist_ok=True)

            if old_str == "" and start_line is None:
                full_path.write_text(new_str, encoding="utf-8")
                return {"path": str(full_path), "action": "created_file"}

            if not full_path.exists():
                return {"error": f"File not found: {path}"}

            original = full_path.read_text(encoding="utf-8")
            file_lines = original.splitlines()
            trailing_newline = original.endswith("\n")

            if start_line is not None:
                s = max(0, start_line - 1)
                e = min(len(file_lines), end_line if end_line is not None else start_line)
                replacement = new_str.splitlines() if new_str else []
                result = file_lines[:s] + replacement + file_lines[e:]
                suffix = "\n" if trailing_newline else ""
                full_path.write_text("\n".join(result) + suffix, encoding="utf-8")
                return {
                    "path": str(full_path),
                    "action": "edited",
                    "method": "line_range",
                    "replaced_lines": f"{start_line}-{end_line or start_line}",
                }

            if old_str in original:
                full_path.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
                return {"path": str(full_path), "action": "edited", "method": "exact"}

            old_lines = old_str.splitlines()
            n = len(old_lines)
            match_start = None
            if n:
                for i in range(len(file_lines) - n + 1):
                    if all(
                        file_lines[i + j].strip() == old_lines[j].strip()
                        for j in range(n)
                    ):
                        match_start = i
                        break

            if match_start is not None:
                result = file_lines[:match_start] + new_str.splitlines() + file_lines[match_start + n:]
                suffix = "\n" if trailing_newline else ""
                full_path.write_text("\n".join(result) + suffix, encoding="utf-8")
                return {"path": str(full_path), "action": "edited", "method": "fuzzy"}

            return {"path": str(full_path), "action": "old_str_not_found"}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    TREE_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", ".indusbuilt", "build", "dist", ".next", ".nuxt"}

    def tree_tool(path: str = ".", depth: int = 3) -> Dict[str, Any]:
        try:
            root = resolve_sandboxed_path(path, sandbox_root)
            if not root.is_dir():
                return {"error": f"Not a directory: {path}"}

            lines: List[str] = []
            total_files = 0
            truncated = False

            def _walk(current: Path, prefix: str, current_depth: int) -> None:
                nonlocal total_files, truncated
                if current_depth > depth:
                    return
                try:
                    entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
                except PermissionError:
                    return
                shown = [e for e in entries if e.name not in TREE_SKIP]
                for i, entry in enumerate(shown):
                    if total_files >= 200:
                        truncated = True
                        return
                    last = i == len(shown) - 1
                    connector = "`-- " if last else "|-- "
                    label = entry.name + ("/" if entry.is_dir() else "")
                    lines.append(prefix + connector + label)
                    if entry.is_file():
                        total_files += 1
                    elif entry.is_dir():
                        extension = "    " if last else "|   "
                        _walk(entry, prefix + extension, current_depth + 1)

            lines.append(root.name + "/")
            _walk(root, "", 1)
            result: Dict[str, Any] = {"tree": "\n".join(lines), "total_files_shown": total_files}
            if truncated:
                result["truncated"] = True
                result["note"] = "Tree truncated at 200 entries. Use search_files or list_files to explore further."
            return result
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    def search_files_tool(pattern: str, path: str = ".") -> Dict[str, Any]:
        import fnmatch
        try:
            root = resolve_sandboxed_path(path, sandbox_root)
            matches: List[str] = []
            for p in sorted(root.rglob("*")):
                if any(part in TREE_SKIP for part in p.relative_to(root).parts):
                    continue
                if p.is_file() and fnmatch.fnmatch(p.name, pattern.split("/")[-1]):
                    if fnmatch.fnmatch(str(p.relative_to(root)), pattern):
                        matches.append(str(p.relative_to(sandbox_root)))
                elif p.is_file() and fnmatch.fnmatch(str(p.relative_to(root)), pattern):
                    matches.append(str(p.relative_to(sandbox_root)))
                if len(matches) >= 100:
                    break
            result: Dict[str, Any] = {"matches": matches, "count": len(matches)}
            if len(matches) == 100:
                result["truncated"] = True
            return result
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    def grep_tool(pattern: str, path: str = ".", include: str = "*") -> Dict[str, Any]:
        import re, fnmatch
        try:
            root = resolve_sandboxed_path(path, sandbox_root)
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return {"error": f"Invalid regex: {e}"}

            hits: List[Dict[str, Any]] = []
            files_searched = 0

            for p in sorted(root.rglob("*")):
                if any(part in TREE_SKIP for part in p.relative_to(root).parts):
                    continue
                if not p.is_file():
                    continue
                if not fnmatch.fnmatch(p.name, include):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                files_searched += 1
                for lineno, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        hits.append({
                            "file": str(p.relative_to(sandbox_root)),
                            "line": lineno,
                            "text": line.strip()[:120],
                        })
                        if len(hits) >= 50:
                            break
                if len(hits) >= 50:
                    break

            result: Dict[str, Any] = {
                "matches": hits,
                "match_count": len(hits),
                "files_searched": files_searched,
            }
            if len(hits) == 50:
                result["truncated"] = True
                result["note"] = "Truncated at 50 matches. Narrow the pattern or path to get more specific results."
            return result
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    tools = {
        "read_file":    read_file_tool,
        "read_files":   read_files_tool,
        "list_files":   list_files_tool,
        "tree":         tree_tool,
        "search_files": search_files_tool,
        "grep":         grep_tool,
        "edit_file":    edit_file_tool,
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
            "name": "read_file",
            "description": (
                "Read lines from a file in the sandbox. Returns 50 lines by default starting at offset 0. "
                "When the response includes has_more=true, call again with next_offset to read further. "
                "Use limit to read more lines at once if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Relative path of the file to read."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (0-indexed). Defaults to 0.",
                        "default": 0
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of lines to read. Defaults to 50. Increase if you need more context.",
                        "default": 50
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_files",
            "description": (
                "Read multiple files in a single call. Returns the first `limit` lines of each file. "
                "Use this instead of calling read_file repeatedly when you need context from several files at once. "
                "Files with has_more=true can be continued with read_file using next_offset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filenames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of relative file paths to read."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Lines to read per file. Defaults to 50.",
                        "default": 50
                    }
                },
                "required": ["filenames"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lists files and directories inside a single directory (one level only). Use tree for a full recursive overview.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path to list. Defaults to '.'."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tree",
            "description": (
                "Show a recursive directory tree of the project. "
                "Use this first when exploring an unfamiliar codebase — it gives you the full structure "
                "so you can decide which files to read. Skips noise dirs (.git, node_modules, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Root directory to tree. Defaults to '.'.",
                        "default": "."
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Max depth to recurse. Defaults to 3.",
                        "default": 3
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Find files by name or glob pattern (e.g. '*.py', '**/*.test.ts', 'config.*'). "
                "Use when you know what kind of file you're looking for but not exactly where it is."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match against file paths (e.g. '*.py', '**/*.json')."
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in. Defaults to '.'.",
                        "default": "."
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search file contents for a pattern (string or regex). "
                "Returns matching lines with file path and line number. "
                "Use to find where a function is defined, where a variable is used, "
                "which files import a module, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "String or regex pattern to search for."
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in. Defaults to '.'.",
                        "default": "."
                    },
                    "include": {
                        "type": "string",
                        "description": "Filename glob to filter which files are searched (e.g. '*.py', '*.ts'). Defaults to '*'.",
                        "default": "*"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Create or edit a file. Three modes:\n"
                "1. CREATE — omit old_str and start_line: writes new_str as the full file.\n"
                "2. LINE RANGE — provide start_line (1-indexed, inclusive) and optionally end_line: "
                "replaces those lines with new_str. Pair with read_file line numbers.\n"
                "3. STRING REPLACE — provide old_str: finds the first match and replaces it with new_str. "
                "Tries exact match first, then falls back to whitespace-tolerant line matching. "
                "Always read the file before editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file to create or edit."
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement content. For CREATE, the full file. For edits, the new block."
                    },
                    "old_str": {
                        "type": "string",
                        "description": "String to find and replace (mode 3). Omit or use '' for CREATE."
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to replace, 1-indexed inclusive (mode 2)."
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to replace, 1-indexed inclusive (mode 2). Defaults to start_line."
                    }
                },
                "required": ["path", "new_str"]
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


# ── System prompt ─────────────────────────────────────────────────────────────
def build_system_prompt(
    sandbox_root: Path,
    skill_registry: Optional[SkillRegistry] = None,
    subagent_registry: Optional[SubAgentRegistry] = None,
) -> str:
    prompt = f"""You are IndusBuilt, an expert coding assistant.

Sandbox directory: {sandbox_root}

IMPORTANT RULES:
- You can ONLY read, list, or edit files INSIDE the sandbox directory above.
- Never attempt to access files outside of the sandbox.
- When creating code, follow best practices and write clean, documented code.
- Always read a file before editing it unless you are creating a new one.
- Chain tool calls as needed to complete tasks (read → understand → edit).
- When done, give a clear summary of what you changed.

SUBAGENTS:
- Use call_subagent to delegate research, exploration, or analysis to a specialized agent.
- Call call_subagent multiple times in the same response to run subagents IN PARALLEL — all run
  concurrently and their results are returned together before you continue.
- Use subagents when you need to gather context from multiple parts of the codebase at once,
  or when exploration and analysis can happen independently before you act.

NAVIGATION (use these to find the right files before reading):
- tree         – full recursive project structure at a glance (start here for unfamiliar codebases)
- search_files – find files by name/glob pattern (e.g. '**/*.py', 'config.*')
- grep         – search file contents by string/regex, returns file+line (find where a function is defined, etc.)
- list_files   – single-directory listing

READING:
- read_file    – read one file (50 lines default, use offset/limit to page through large files)
- read_files   – read multiple files in one call (use after navigation to load relevant files)

EDITING:
- edit_file    – create files or patch with string-replace or line-range replace

MEMORY:
- save_memory, search_memory, retrieve_code, summarize_session, offload_large_output

SUBAGENTS:
- call_subagent – delegate tasks to specialized subagents (call multiple times to run in parallel)

WORKFLOW for large codebases:
1. tree / search_files / grep  → identify relevant files
2. read_files                  → load them all at once
3. edit_file                   → make changes
"""

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
        return {
            "provider": provider,
            "model": model,
            "subagent_model": subagent_model,
            "api_key": api_key,
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
            self._emit(Info(
                f"Provider: {state['provider']}  |  Model: {state['model']}  |  "
                f"SubAgent Model: {state['subagent_model']}"
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

        matcher = self._ask_input("Matcher regex (leave empty for all tools)", "e.g. edit_file") or None
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

    # ── Streaming ───────────────────────────────────────────────────────────
    def _stream_model_turn(self) -> Dict[str, Any]:
        state = self._refresh_runtime_state()
        validation_error = _validate_api_key(state["provider"], state["api_key"])
        if validation_error:
            raise ValueError(validation_error)

        self._emit(Thinking(label="thinking"))
        streamed_text_parts: List[str] = []
        pending_tool_calls: Dict[int, Dict[str, Any]] = {}
        assistant_started = False

        try:
            stream = completion(
                model=_provider_model_ref(state["provider"], state["model"]),
                messages=self.context_manager.build_messages(
                    system_prompt=build_system_prompt(
                        self.sandbox_root, self.skill_registry, self.subagent_registry
                    ),
                    conversation=self.conversation,
                ),
                tools=build_openai_tools(
                    self.skill_registry,
                    context_manager=self.context_manager,
                    subagent_registry=self.subagent_registry,
                ),
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

            self._emit_code_diff_if_any(tool_name, args, result)

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

    def _emit_code_diff_if_any(self, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        if tool_name != "edit_file":
            return
        if "error" in result:
            return
        path = result.get("path")
        action = result.get("action")
        if not path or action not in ("created_file", "edited"):
            return
        try:
            new_content = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            new_content = ""

        before = ""
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        if action == "edited" and old_str:
            before = old_str
        diff_text = _format_diff(before, new_str or new_content, max_lines=80)
        self._emit(CodeDiff(
            path=str(path),
            action=action,
            before=before or None,
            after=new_str or new_content,
            diff_text=diff_text,
        ))

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

            # Inner agentic loop
            while True:
                if self._cancelled.is_set():
                    break
                try:
                    turn = self._stream_model_turn()
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
