"""
IndusBuilt Coding Agent Core
"""
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from litellm import completion
from .context_manager import ContextManager
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
from .ui import (
    Spinner,
    choose_from_list,
    print_error,
    print_assistant_prefix,
    print_runtime_meta,
    print_startup_banner,
    print_success,
    print_slash_help,
    print_tool_call,
    print_subagent_dispatch,
    print_subagent_result,
    print_user_prompt,
)


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

    # Enforce sandbox boundary
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
        """
        Lists all files and directories inside a given directory.
        Only paths within the current working (sandbox) directory are accessible.

        :param path: The relative path of the directory to list. Defaults to '.'.
        :return: A list of files and directories with their types.
        """
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

            # Mode 1: create / full overwrite
            if old_str == "" and start_line is None:
                full_path.write_text(new_str, encoding="utf-8")
                return {"path": str(full_path), "action": "created_file"}

            if not full_path.exists():
                return {"error": f"File not found: {path}"}

            original = full_path.read_text(encoding="utf-8")
            file_lines = original.splitlines()
            trailing_newline = original.endswith("\n")

            # Mode 2: line-range replace (1-indexed, end_line inclusive)
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

            # Mode 3a: exact string match
            if old_str in original:
                full_path.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
                return {"path": str(full_path), "action": "edited", "method": "exact"}

            # Mode 3b: fuzzy fallback — compare lines ignoring leading whitespace
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


# ── Agent loop ────────────────────────────────────────────────────────────────
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

    # Soft prefix validation for clearer UX.
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


def _select_provider_interactive(current_provider: str) -> str:
    labels = [f"{provider} {'(active)' if provider == current_provider else ''}".rstrip() for provider in PROVIDERS]
    selected = choose_from_list("Select Provider", labels)
    return PROVIDERS[selected]


def _select_model_interactive(provider: str, current_model: str) -> str:
    choices = MODEL_CHOICES.get(provider, [current_model])
    labels = [f"{model} {'(current)' if model == current_model else ''}".rstrip() for model in choices]
    selected = choose_from_list(f"Select Model ({provider})", labels)
    return choices[selected]


def _select_subagent_model_interactive(provider: str, current_model: str) -> str:
    choices = SUBAGENT_MODEL_CHOICES.get(provider, [current_model])
    labels = [f"{model} {'(current)' if model == current_model else ''}".rstrip() for model in choices]
    selected = choose_from_list(f"Select SubAgent Model ({provider})", labels)
    return choices[selected]


def _set_key_interactive(settings: Dict[str, Any], provider: Optional[str] = None) -> None:
    target_provider = provider or _select_provider_interactive(get_active_provider(settings))
    entered = getpass.getpass(f"Enter {target_provider} API key: ").strip()
    if not entered:
        print_error("Empty key. Nothing saved.")
        return

    validation_error = _validate_api_key(target_provider, entered)
    if validation_error:
        print_error(validation_error)
        return

    set_api_key(settings, target_provider, entered)
    save_settings(settings)
    print_success(f"Saved key for provider '{target_provider}'.")


def _ensure_active_provider_key(settings: Dict[str, Any]) -> str:
    while True:
        provider = get_active_provider(settings)
        existing = _get_effective_api_key(settings, provider)
        if existing:
            validation_error = _validate_api_key(provider, existing)
            if validation_error is None:
                return existing

            print_error(validation_error)
            choice = choose_from_list(
                "Provider Key Invalid",
                [
                    "Re-enter key for current provider",
                    "Switch provider",
                    "Exit",
                ],
            )

            if choice == 0:
                _set_key_interactive(settings, provider=provider)
                continue
            if choice == 1:
                selected = _select_provider_interactive(provider)
                set_active_provider(settings, selected)
                save_settings(settings)
                continue
            raise SystemExit(0)

            return existing

        print_error(f"No API key saved for provider '{provider}'.")
        choice = choose_from_list(
            "Provider Key Required",
            [
                "Add key for current provider",
                "Switch provider",
                "Exit",
            ],
        )

        if choice == 0:
            _set_key_interactive(settings, provider=provider)
        elif choice == 1:
            selected = _select_provider_interactive(provider)
            set_active_provider(settings, selected)
            save_settings(settings)
        else:
            raise SystemExit(0)


def run_agent(sandbox_root: Path, settings: Dict[str, Any]):
    skill_registry = SkillRegistry(sandbox_root)
    skill_registry.refresh()
    skill_commands = SkillCommandHandler(skill_registry)
    subagent_registry = SubAgentRegistry(sandbox_root)
    subagent_registry.refresh()
    subagent_commands = SubAgentCommandHandler(subagent_registry)
    conversation: List[Dict] = []
    context_manager = ContextManager(sandbox_root)
    tool_registry = make_tools(
        sandbox_root,
        skill_registry=skill_registry,
        context_manager=context_manager,
        conversation_ref=conversation,
    )
    active_provider = get_active_provider(settings)
    active_model = get_model(settings, active_provider)

    print_startup_banner(sandbox_root=sandbox_root, model=active_model)
    print_runtime_meta(provider=active_provider, model=active_model)

    def refresh_runtime_state() -> Dict[str, str]:
        provider = get_active_provider(settings)
        model = get_model(settings, provider)
        subagent_model = get_subagent_model(settings, provider)
        api_key = _get_effective_api_key(settings, provider)
        return {
            "provider": provider,
            "model": model,
            "subagent_model": subagent_model,
            "api_key": api_key,
        }

    def handle_slash_command(command: str) -> bool:
        raw_command = command.strip()
        raw = raw_command.lower()

        if raw in ("/", "/menu"):
            selected = choose_from_list(
                "IndusBuilt Command Palette",
                [
                    "Set API key",
                    "Switch provider",
                    "Change model",
                    "Change subagent model",
                    "Show current settings",
                    "Memory status",
                    "Skills",
                    "Create skill",
                    "SubAgents",
                    "Create subagent",
                    "Help",
                    "Cancel",
                ],
            )
            if selected == 0:
                _set_key_interactive(settings)
            elif selected == 1:
                provider_before = get_active_provider(settings)
                selected_provider = _select_provider_interactive(provider_before)
                set_active_provider(settings, selected_provider)
                save_settings(settings)
                print_success(f"Active provider set to {selected_provider}.")
            elif selected == 2:
                provider = get_active_provider(settings)
                current_model = get_model(settings, provider)
                selected_model = _select_model_interactive(provider, current_model)
                set_model(settings, provider, selected_model)
                save_settings(settings)
                print_success(f"Model for {provider} set to {selected_model}.")
            elif selected == 3:
                provider = get_active_provider(settings)
                current_sa_model = get_subagent_model(settings, provider)
                selected_sa_model = _select_subagent_model_interactive(provider, current_sa_model)
                set_subagent_model(settings, provider, selected_sa_model)
                save_settings(settings)
                print_success(f"SubAgent model for {provider} set to {selected_sa_model}.")
            elif selected == 4:
                state = refresh_runtime_state()
                print_runtime_meta(provider=state["provider"], model=state["model"])
                print(f"  SubAgent model: {state['subagent_model']}")
            elif selected == 5:
                status = context_manager.status()
                print(json.dumps(status, indent=2, ensure_ascii=False))
            elif selected == 6:
                skill_commands.handle_skills_command("/skills")
            elif selected == 7:
                skill_commands.handle_create_skill_command("/create skills")
            elif selected == 8:
                subagent_commands.handle_subagents_command("/subagents")
            elif selected == 9:
                subagent_commands.handle_create_subagent_command("/create subagent")
            elif selected == 10:
                print_slash_help()
            return True

        if raw.startswith("/key"):
            _set_key_interactive(settings)
            return True

        if raw.startswith("/provider"):
            provider_before = get_active_provider(settings)
            selected_provider = _select_provider_interactive(provider_before)
            set_active_provider(settings, selected_provider)
            save_settings(settings)
            print_success(f"Active provider set to {selected_provider}.")
            return True

        if raw.startswith("/model"):
            provider = get_active_provider(settings)
            current_model = get_model(settings, provider)
            selected_model = _select_model_interactive(provider, current_model)
            set_model(settings, provider, selected_model)
            save_settings(settings)
            print_success(f"Model for {provider} set to {selected_model}.")
            return True

        if raw.startswith("/show"):
            state = refresh_runtime_state()
            print_runtime_meta(provider=state["provider"], model=state["model"])
            return True

        if raw.startswith("/skills"):
            skill_commands.handle_skills_command(raw_command)
            return True

        if raw.startswith("/subagent model") or raw == "/subagent model":
            provider = get_active_provider(settings)
            current_sa_model = get_subagent_model(settings, provider)
            selected_sa_model = _select_subagent_model_interactive(provider, current_sa_model)
            set_subagent_model(settings, provider, selected_sa_model)
            save_settings(settings)
            print_success(f"SubAgent model for {provider} set to {selected_sa_model}.")
            return True

        if raw.startswith("/subagents"):
            subagent_commands.handle_subagents_command(raw_command)
            return True

        if raw.startswith("/memory"):
            parts = raw_command.strip().split(maxsplit=2)
            action = parts[1].lower() if len(parts) > 1 else "status"
            if action in ("status", "show"):
                print(json.dumps(context_manager.status(), indent=2, ensure_ascii=False))
                return True
            if action == "search":
                query = parts[2].strip() if len(parts) > 2 else ""
                if not query:
                    print_error("Usage: /memory search <query>")
                    return True
                print(json.dumps(context_manager.search_memory(query=query), indent=2, ensure_ascii=False))
                return True
            if action == "summarize":
                result = context_manager.summarize_session(conversation, reason="manual")
                print_success(f"Session summarized to {result['path']}")
                return True
            if action == "rebuild":
                result = context_manager.rebuild_index()
                print_success(f"Memory index rebuilt with {result['count']} entries.")
                return True
            print_error("Unknown /memory command. Use: status, search, summarize, rebuild")
            return True

        if raw.startswith("/create skill"):
            skill_commands.handle_create_skill_command(raw_command)
            return True

        if raw.startswith("/create subagent"):
            subagent_commands.handle_create_subagent_command(raw_command)
            return True

        if raw.startswith("/help"):
            print_slash_help()
            return True

        if raw.startswith("/exit"):
            print("Goodbye!")
            raise SystemExit(0)

        return False

    def stream_model_turn() -> Dict[str, Any]:
        """Stream a model turn and collect both text and tool calls."""
        state = refresh_runtime_state()
        validation_error = _validate_api_key(state["provider"], state["api_key"])
        if validation_error:
            raise ValueError(validation_error)

        streamed_text_parts: List[str] = []
        pending_tool_calls: Dict[int, Dict[str, Any]] = {}
        assistant_started = False

        spinner_stopped = False
        with Spinner("thinking") as spinner:
            stream = completion(
                model=_provider_model_ref(state["provider"], state["model"]),
                messages=context_manager.build_messages(
                    system_prompt=build_system_prompt(sandbox_root, skill_registry, subagent_registry),
                    conversation=conversation,
                ),
                tools=build_openai_tools(skill_registry, context_manager=context_manager, subagent_registry=subagent_registry),
                stream=True,
                api_key=state["api_key"],
            )

            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = choices[0].delta

                if not spinner_stopped and (delta.content or delta.tool_calls):
                    spinner.stop()
                    spinner_stopped = True

                if delta.content:
                    if not assistant_started:
                        print_assistant_prefix()
                        assistant_started = True
                    print(delta.content, end="", flush=True)
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

        if assistant_started:
            print("\n")

        ordered_calls = [pending_tool_calls[i] for i in sorted(pending_tool_calls.keys())]
        return {
            "content": "".join(streamed_text_parts),
            "tool_calls": ordered_calls,
        }

    while True:
        try:
            user_input = input(print_user_prompt()).strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            sys.exit(0)

        if user_input.lower() in ("exit", "quit", "bye"):
            print("Goodbye!")
            sys.exit(0)

        if not user_input:
            continue

        if user_input.startswith("/"):
            try:
                handled = handle_slash_command(user_input)
            except SystemExit:
                raise
            except Exception as cmd_err:
                print_error(f"Command error: {cmd_err}")
                handled = True
            if handled:
                continue

        try:
            _ensure_active_provider_key(settings)
        except SystemExit:
            print("Goodbye!")
            sys.exit(0)

        conversation.append({"role": "user", "content": user_input})
        context_manager.register_user_turn(user_input)

        # ── inner agentic loop ────────────────────────────────────────────────
        while True:
            try:
                turn = stream_model_turn()
            except Exception as e:
                print_error(f"Provider API error: {e}")
                break

            assistant_text: str = turn.get("content", "") or ""
            tool_calls: List[Dict[str, Any]] = turn.get("tool_calls", []) or []

            # No tool call → final assistant reply
            if not tool_calls:
                if not assistant_text:
                    print_assistant_prefix()
                    print("Done.\n")
                conversation.append({"role": "assistant", "content": assistant_text})
                context_manager.maybe_auto_summarize(conversation)
                break

            # There are tool calls to execute
            # Add assistant message (with tool_calls) to conversation
            conversation.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": tool_calls,
            })

            # ── Separate subagent calls from regular tool calls ───────────────
            regular_tcs = [
                tc for tc in tool_calls
                if tc.get("function", {}).get("name") != "call_subagent"
            ]
            subagent_tcs = [
                tc for tc in tool_calls
                if tc.get("function", {}).get("name") == "call_subagent"
            ]

            tc_results: Dict[str, Any] = {}

            # Execute regular tools sequentially
            for tc in regular_tcs:
                tool_name = tc.get("function", {}).get("name", "")
                raw_arguments: Optional[str] = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                fn = tool_registry.get(tool_name)
                if fn:
                    with Spinner(f"{tool_name} in progress"):
                        result = fn(**args)
                else:
                    result = {"error": f"Unknown tool: {tool_name}"}

                result = context_manager.maybe_offload_tool_result(tool_name, result)
                context_manager.register_tool_result(tool_name, result)
                print_tool_call(tool_name=tool_name, args=args, result=result)
                tc_results[tc.get("id", "")] = result

            # Execute subagent calls in parallel
            if subagent_tcs:
                state = refresh_runtime_state()
                valid_sa_calls: List[Any] = []

                for tc in subagent_tcs:
                    raw_args: Optional[str] = tc.get("function", {}).get("arguments", "{}")
                    try:
                        sa_args = json.loads(raw_args or "{}")
                    except json.JSONDecodeError:
                        sa_args = {}

                    sa_name = sa_args.get("subagent", "")
                    task = sa_args.get("task", "")
                    tc_id = tc.get("id", "")
                    sa_def = subagent_registry.get(sa_name)

                    if sa_def:
                        valid_sa_calls.append((sa_def, task, tc_id))
                    else:
                        available = [a.name for a in subagent_registry.list_agents()]
                        tc_results[tc_id] = {
                            "error": f"Unknown subagent: '{sa_name}'",
                            "available": available,
                        }

                if valid_sa_calls:
                    dispatch_info = [(defn.name, task) for defn, task, _ in valid_sa_calls]
                    print_subagent_dispatch(dispatch_info)

                    calls_for_runner = [(defn, task) for defn, task, _ in valid_sa_calls]
                    plural = "s" if len(calls_for_runner) > 1 else ""
                    with Spinner(f"running {len(calls_for_runner)} subagent{plural} in parallel"):
                        sa_results = run_subagents_parallel(
                            calls_for_runner,
                            tool_registry,
                            build_openai_tools(skill_registry, context_manager=context_manager),
                            state["provider"],
                            state["subagent_model"],
                            state["api_key"],
                        )

                    for (defn, task, tc_id), sa_result in zip(valid_sa_calls, sa_results):
                        print_subagent_result(sa_result)
                        result_dict: Dict[str, Any] = {
                            "subagent": sa_result.name,
                            "output": sa_result.output,
                            "elapsed_s": round(sa_result.elapsed_s, 2),
                            "turns": sa_result.turns,
                        }
                        if sa_result.error:
                            result_dict["error"] = sa_result.error
                        tc_results[tc_id] = result_dict

            # Append all results to conversation in original order
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                result = tc_results.get(tc_id, {"error": "Result not collected"})
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
                context_manager.maybe_auto_summarize(conversation)
