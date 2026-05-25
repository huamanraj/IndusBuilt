"""
SubAgent system for IndusBuilt.
Specialized agents the main agent can delegate tasks to — including parallel execution.
"""
from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from litellm import completion

SUBAGENT_FILE_NAME = "AGENT.md"
SUBAGENT_SEARCH_DIRS = (".indusbuilt/subagents", ".agents/subagents")

BUILTIN_SUBAGENTS: Dict[str, Dict[str, Any]] = {
    "explore": {
        "name": "explore",
        "description": (
            "Fast read-only agent optimized for searching and analyzing codebases. "
            "Use for locating files, finding symbol definitions, tracing call paths, "
            "and understanding code structure. Returns a concise structured report."
        ),
        "system_prompt": (
            "You are Explore, a fast read-only codebase search subagent.\n"
            "Your job: quickly find, trace, and summarize code — then return a concise report.\n\n"
            "Guidelines:\n"
            "- Use list_files to navigate directory structure\n"
            "- Use read_file to inspect relevant source files\n"
            "- Use retrieve_code for keyword-based snippet search\n"
            "- Return a clear, structured summary of what you found\n"
            "- Include file paths and line references where relevant\n"
            "- Be fast and precise — the main agent depends on your findings\n"
            "- Do NOT make any edits or create any files"
        ),
        "tools": ["read_file", "list_files", "retrieve_code", "search_memory"],
        "max_turns": 8,
        "source": "builtin",
    },
    "research": {
        "name": "research",
        "description": (
            "Context-gathering agent for planning. Use during plan mode to understand "
            "existing patterns, conventions, architecture, and dependencies before "
            "presenting a solution. Returns thorough architectural context."
        ),
        "system_prompt": (
            "You are Research, a context-gathering subagent used during planning.\n"
            "Your job: deeply understand the codebase and return thorough, actionable context.\n\n"
            "Guidelines:\n"
            "- Read key files to understand architecture, patterns, and conventions\n"
            "- Search memory for relevant past decisions and known bugs\n"
            "- Trace imports and dependencies to map the relevant subsystem\n"
            "- Return: key patterns, relevant file paths, design constraints, important context\n"
            "- Be thorough — your findings directly shape the plan\n"
            "- Do NOT make any edits or create any files"
        ),
        "tools": ["read_file", "list_files", "retrieve_code", "search_memory"],
        "max_turns": 12,
        "source": "builtin",
    },
}


@dataclass
class SubAgentDef:
    """Definition of a subagent (built-in or custom)."""
    name: str
    description: str
    system_prompt: str
    tools: List[str]
    max_turns: int = 8
    source: str = "builtin"


@dataclass
class SubAgentResult:
    """Result returned by a completed subagent run."""
    name: str
    task: str
    output: str
    elapsed_s: float
    turns: int
    error: Optional[str] = None


class SubAgentRegistry:
    """Manages built-in and custom subagent definitions."""

    def __init__(self, sandbox_root: Path):
        self.sandbox_root = sandbox_root.resolve()
        self._agents: Dict[str, SubAgentDef] = {}
        self._load_builtins()

    def _load_builtins(self) -> None:
        for cfg in BUILTIN_SUBAGENTS.values():
            self._agents[cfg["name"]] = SubAgentDef(**cfg)

    def refresh(self) -> None:
        """Reload builtins and scan project directories for custom subagents."""
        self._load_builtins()
        for rel in SUBAGENT_SEARCH_DIRS:
            root = (self.sandbox_root / rel).resolve()
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                agent_file = child / SUBAGENT_FILE_NAME
                if agent_file.is_file():
                    sa = self._parse_agent_file(agent_file)
                    if sa:
                        self._agents[sa.name] = sa

    def _parse_agent_file(self, path: Path) -> Optional[SubAgentDef]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return None

        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return None

        end_fm: Optional[int] = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_fm = i
                break
        if end_fm is None:
            return None

        fm: Dict[str, str] = {}
        for line in lines[1:end_fm]:
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip("\"'")

        body = "\n".join(lines[end_fm + 1:]).strip()
        name = fm.get("name", path.parent.name).strip()
        description = fm.get("description", "").strip()
        if not name or not description:
            return None

        tools_raw = fm.get("tools", "read_file,list_files,retrieve_code")
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
        max_turns = int(fm.get("max_turns", "8"))

        return SubAgentDef(
            name=name,
            description=description,
            system_prompt=body,
            tools=tools,
            max_turns=max_turns,
            source=str(path),
        )

    def list_agents(self) -> List[SubAgentDef]:
        return list(self._agents.values())

    def get(self, name: str) -> Optional[SubAgentDef]:
        return self._agents.get(name)

    def names(self) -> List[str]:
        return list(self._agents.keys())

    def catalog_prompt(self) -> str:
        if not self._agents:
            return ""
        entries = [
            f"  <subagent>\n"
            f"    <name>{sa.name}</name>\n"
            f"    <description>{sa.description}</description>\n"
            f"  </subagent>"
            for sa in self.list_agents()
        ]
        return (
            "You can delegate tasks to specialized subagents using call_subagent. "
            "Calling it multiple times in the same response runs subagents in parallel.\n"
            "<available_subagents>\n"
            + "\n".join(entries)
            + "\n</available_subagents>"
        )

    def call_schema(self) -> Dict[str, Any]:
        """OpenAI tool schema for the call_subagent tool."""
        return {
            "type": "function",
            "function": {
                "name": "call_subagent",
                "description": (
                    "Delegate a task to a specialized subagent. "
                    "Call this tool multiple times in the same response to run subagents in parallel — "
                    "all parallel calls complete before their results are returned to you. "
                    "Each subagent returns a detailed report of its findings."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subagent": {
                            "type": "string",
                            "enum": self.names(),
                            "description": "Name of the subagent to delegate to.",
                        },
                        "task": {
                            "type": "string",
                            "description": (
                                "Specific task description for this subagent. "
                                "Be precise about what to find, analyze, or return."
                            ),
                        },
                    },
                    "required": ["subagent", "task"],
                },
            },
        }

    def create_custom(
        self, name: str, description: str, purpose: str
    ) -> Dict[str, Any]:
        """Scaffold a new custom subagent in the project directory."""
        from .skills import normalize_skill_name

        slug = normalize_skill_name(name)
        if not slug:
            return {"error": "Invalid name. Use letters, numbers, hyphens, or underscores."}

        target_dir = (self.sandbox_root / ".indusbuilt" / "subagents" / slug).resolve()
        target_file = target_dir / SUBAGENT_FILE_NAME
        if target_file.exists():
            return {"error": f"Subagent already exists at: {target_file}"}

        target_dir.mkdir(parents=True, exist_ok=True)
        target_file.write_text(_agent_template(slug, description, purpose), encoding="utf-8")
        self.refresh()
        return {"name": slug, "created": True, "path": str(target_file)}


def _agent_template(name: str, description: str, purpose: str) -> str:
    title = name.replace("-", " ").replace("_", " ").title()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "tools: read_file,list_files,retrieve_code,search_memory\n"
        "max_turns: 8\n"
        "---\n\n"
        f"# {title} SubAgent\n\n"
        f"You are a specialized subagent for: {purpose}\n\n"
        "## Instructions\n\n"
        "- Describe your specialized workflow and instructions here.\n"
        "- Return a concise, structured summary of your findings.\n"
        "- Do NOT edit or create files unless the purpose above explicitly requires it.\n"
    )


def run_subagent(
    defn: SubAgentDef,
    task: str,
    tool_registry: Dict[str, Callable],
    all_tool_schemas: List[Dict[str, Any]],
    provider: str,
    model: str,
    api_key: str,
) -> SubAgentResult:
    """Run a single subagent synchronously. Safe to call from a worker thread."""
    allowed = set(defn.tools)
    sub_registry = {k: v for k, v in tool_registry.items() if k in allowed}
    sub_schemas = [
        s for s in all_tool_schemas
        if s.get("function", {}).get("name") in allowed
    ]

    messages: List[Dict[str, Any]] = [{"role": "user", "content": task}]
    system_msg = {"role": "system", "content": defn.system_prompt}
    output_parts: List[str] = []
    turns = 0
    start = time.time()

    try:
        while turns < defn.max_turns:
            turns += 1
            resp = completion(
                model=f"{provider}/{model}",
                messages=[system_msg] + messages,
                tools=sub_schemas if sub_schemas else None,
                api_key=api_key,
            )

            msg = resp.choices[0].message
            content: str = msg.content or ""
            if content:
                output_parts.append(content)

            raw_tool_calls = getattr(msg, "tool_calls", None) or []
            if not raw_tool_calls:
                break

            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in raw_tool_calls
                ],
            })

            for tc in raw_tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}

                fn = sub_registry.get(fn_name)
                if fn:
                    try:
                        result = fn(**args)
                    except Exception as exc:
                        result = {"error": str(exc)}
                else:
                    result = {"error": f"Tool '{fn_name}' is not available to subagent '{defn.name}'"}

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

    except Exception as exc:
        return SubAgentResult(
            name=defn.name,
            task=task,
            output="",
            elapsed_s=time.time() - start,
            turns=turns,
            error=str(exc),
        )

    return SubAgentResult(
        name=defn.name,
        task=task,
        output="\n".join(output_parts) or "(no output produced)",
        elapsed_s=time.time() - start,
        turns=turns,
    )


def run_subagents_parallel(
    calls: List[Tuple[SubAgentDef, str]],
    tool_registry: Dict[str, Callable],
    all_tool_schemas: List[Dict[str, Any]],
    provider: str,
    model: str,
    api_key: str,
) -> List[SubAgentResult]:
    """Run multiple subagents in parallel. Results are returned in the same order as calls."""
    if not calls:
        return []
    if len(calls) == 1:
        defn, task = calls[0]
        return [run_subagent(defn, task, tool_registry, all_tool_schemas, provider, model, api_key)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [
            pool.submit(run_subagent, defn, task, tool_registry, all_tool_schemas, provider, model, api_key)
            for defn, task in calls
        ]
        return [f.result() for f in futures]
