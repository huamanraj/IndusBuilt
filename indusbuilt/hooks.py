"""
Agent Hooks system for IndusBuilt.
Lifecycle hooks that intercept and control agent behavior at key execution points.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .settings import get_active_provider, get_api_key, get_model
from .subagents import SubAgentResult


HOOK_CONFIG_FILE = "hooks.json"
HOOK_SEARCH_DIRS = (
    ".indusbuilt/hooks",
    ".agents/hooks",
)


# ── Hook event types ──────────────────────────────────────────────────────────

class HookEvent(str, Enum):
    """Lifecycle events that can trigger hooks."""
    SessionStart = "SessionStart"
    SessionEnd = "SessionEnd"
    UserPromptSubmit = "UserPromptSubmit"
    PreToolUse = "PreToolUse"
    PostToolUse = "PostToolUse"
    PostToolUseFailure = "PostToolUseFailure"
    Stop = "Stop"
    SubagentStart = "SubagentStart"
    SubagentStop = "SubagentStop"


# ── Hook types ────────────────────────────────────────────────────────────────

class HookType(str, Enum):
    """Types of hooks that can be configured."""
    command = "command"
    prompt = "prompt"
    agent = "agent"


# ── PreToolUse permission decisions ───────────────────────────────────────────

class PermissionDecision(str, Enum):
    """Decisions a PreToolUse hook can make."""
    allow = "allow"
    block = "block"
    warn = "warn"
    ask = "ask"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class HookConfig:
    """Configuration for a single hook."""
    name: str
    event: str
    hook_type: str
    matcher: Optional[str] = None
    command: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    timeout: int = 30
    decision_on_deny: str = "block"
    source: str = ""

    def __post_init__(self):
        if self.matcher is not None:
            try:
                self._matcher_re = re.compile(self.matcher, re.IGNORECASE)
            except re.error:
                self._matcher_re = None
        else:
            self._matcher_re = None

    def matches(self, target: str) -> bool:
        if self._matcher_re is None:
            return True
        return bool(self._matcher_re.search(target))


@dataclass
class HookResult:
    """Result returned after executing a hook."""
    hook_name: str
    event: str
    decision: str = "allow"
    reason: str = ""
    modified_input: Optional[Dict[str, Any]] = None
    additional_context: str = ""
    system_message: str = ""
    output: str = ""
    elapsed_s: float = 0.0
    error: Optional[str] = None


@dataclass
class HookEventContext:
    """Context data passed to hook execution."""
    session_id: str = ""
    cwd: str = ""
    hook_event_name: str = ""
    tool_name: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)
    tool_output: Optional[Dict[str, Any]] = None
    user_prompt: str = ""
    conversation: List[Dict[str, Any]] = field(default_factory=list)
    subagent_name: str = ""
    subagent_task: str = ""
    subagent_result: Optional[Dict[str, Any]] = None
    error_message: str = ""


# ── Hook execution engine ─────────────────────────────────────────────────────

def _run_command_hook(command: str, input_json: str, timeout: int, cwd: str) -> Dict[str, Any]:
    """Execute a shell command hook and parse its JSON output."""
    try:
        proc = subprocess.run(
            command,
            input=input_json,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
            cwd=cwd or None,
        )

        output = proc.stdout.strip()
        stderr_out = proc.stderr.strip()

        if not output:
            return {
                "exit_code": proc.returncode,
                "stdout": "",
                "stderr": stderr_out,
                "decision": "block" if proc.returncode == 2 else "allow",
                "reason": stderr_out if proc.returncode == 2 else "",
            }

        try:
            parsed = json.loads(output)
            return {
                "exit_code": proc.returncode,
                "stdout": output,
                "stderr": stderr_out,
                "decision": parsed.get("decision", "allow"),
                "reason": parsed.get("reason", ""),
                "modified_input": parsed.get("modified_input"),
                "additional_context": parsed.get("additional_context", ""),
                "system_message": parsed.get("system_message", ""),
            }
        except json.JSONDecodeError:
            decision = "block" if proc.returncode == 2 else "allow"
            return {
                "exit_code": proc.returncode,
                "stdout": output,
                "stderr": stderr_out,
                "decision": decision,
                "reason": output[:200] if output else "",
            }

    except subprocess.TimeoutExpired:
        return {"error": f"Hook timed out after {timeout}s", "decision": "allow"}
    except Exception as e:
        return {"error": str(e), "decision": "allow"}


def _run_prompt_hook(
    prompt_template: str,
    model: str,
    provider: str,
    api_key: str,
    context: HookEventContext,
    timeout: int,
) -> Dict[str, Any]:
    """Use a fast LLM to evaluate a hook decision."""
    try:
        import litellm

        ctx_json = json.dumps({
            "hook_event": context.hook_event_name,
            "tool_name": context.tool_name,
            "tool_input": context.tool_input,
            "user_prompt": context.user_prompt,
        }, ensure_ascii=False)

        full_prompt = prompt_template.replace("$ARGUMENTS", ctx_json)

        def _call():
            resp = litellm.completion(
                model=f"{provider}/{model}",
                messages=[
                    {"role": "system", "content": "You are a hook evaluator. Respond with exactly one JSON object with 'decision' ('allow', 'block', 'warn', 'ask'), 'reason' (string), and optionally 'modified_input', 'additional_context', 'system_message'. No other text."},
                    {"role": "user", "content": full_prompt},
                ],
                api_key=api_key,
                temperature=0.0,
                max_tokens=500,
            )
            return resp

        result = None
        error = None

        def _worker():
            nonlocal result, error
            try:
                result = _call()
            except Exception as e:
                error = str(e)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            return {"error": f"Prompt hook timed out after {timeout}s", "decision": "allow"}

        if error:
            return {"error": error, "decision": "allow"}

        if result is None:
            return {"error": "No response from model", "decision": "allow"}

        content = result.choices[0].message.content or ""
        content = content.strip()

        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            parsed = json.loads(content)
            return {
                "decision": parsed.get("decision", "allow"),
                "reason": parsed.get("reason", ""),
                "modified_input": parsed.get("modified_input"),
                "additional_context": parsed.get("additional_context", ""),
                "system_message": parsed.get("system_message", ""),
            }
        except (json.JSONDecodeError, IndexError):
            return {"decision": "allow", "reason": "Could not parse hook response"}

    except ImportError:
        return {"error": "litellm not available for prompt hook", "decision": "allow"}


def _run_agent_hook(
    prompt_template: str,
    model: str,
    provider: str,
    api_key: str,
    context: HookEventContext,
    timeout: int,
    tool_registry: Optional[Dict[str, Callable]] = None,
) -> Dict[str, Any]:
    """Spawn a verification subagent to analyze a hook event."""
    try:
        import litellm

        ctx_json = json.dumps({
            "hook_event": context.hook_event_name,
            "tool_name": context.tool_name,
            "tool_input": context.tool_input,
            "tool_output": context.tool_output,
            "user_prompt": context.user_prompt,
        }, ensure_ascii=False)

        full_prompt = prompt_template.replace("$ARGUMENTS", ctx_json)

        max_turns = 6
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": "You are a verification agent. Execute your instructions and return a final JSON with 'decision' ('allow', 'block', 'warn', 'ask'), 'reason', and optionally 'additional_context' or 'system_message'. No other text before the final JSON."},
            {"role": "user", "content": full_prompt},
        ]
        output_parts: List[str] = []

        for _ in range(max_turns):
            resp = litellm.completion(
                model=f"{provider}/{model}",
                messages=messages,
                api_key=api_key,
                temperature=0.0,
                max_tokens=1000,
            )

            msg = resp.choices[0].message
            content = msg.content or ""
            if content:
                output_parts.append(content)

            if msg.tool_calls and tool_registry:
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    } for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    fn = tool_registry.get(tc.function.name)
                    if fn:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                            result = fn(**args)
                        except Exception:
                            result = {"error": "Tool execution failed"}
                    else:
                        result = {"error": f"Unknown tool: {tc.function.name}"}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                break

        full_output = "\n".join(output_parts).strip()

        try:
            if "```json" in full_output:
                json_block = full_output.split("```json")[1].split("```")[0].strip()
            elif "```" in full_output:
                json_block = full_output.split("```")[1].split("```")[0].strip()
            else:
                match = re.search(r'\{[^{}]*"decision"[^{}]*\}', full_output, re.DOTALL)
                json_block = match.group(0) if match else full_output

            parsed = json.loads(json_block)
            return {
                "decision": parsed.get("decision", "allow"),
                "reason": parsed.get("reason", ""),
                "additional_context": parsed.get("additional_context", ""),
                "system_message": parsed.get("system_message", ""),
            }
        except (json.JSONDecodeError, IndexError):
            return {"decision": "allow", "reason": "Could not parse agent hook response"}

    except ImportError:
        return {"error": "litellm not available for agent hook", "decision": "allow"}


def _run_single_hook(
    hook: HookConfig,
    context: HookEventContext,
    settings: Optional[Dict[str, Any]] = None,
    tool_registry: Optional[Dict[str, Callable]] = None,
) -> HookResult:
    """Execute a single hook and return its result."""
    start = time.time()

    try:
        if hook.hook_type == HookType.command:
            input_json = json.dumps({
                "hook_event_name": context.hook_event_name,
                "tool_name": context.tool_name,
                "tool_input": context.tool_input,
                "tool_output": context.tool_output,
                "user_prompt": context.user_prompt,
                "session_id": context.session_id,
                "cwd": context.cwd,
                "subagent_name": context.subagent_name,
                "subagent_task": context.subagent_task,
            }, ensure_ascii=False)

            raw = _run_command_hook(hook.command or "", input_json, hook.timeout, context.cwd)

        elif hook.hook_type == HookType.prompt:
            if settings is None:
                return HookResult(
                    hook_name=hook.name,
                    event=context.hook_event_name,
                    decision="allow",
                    error="No settings available for prompt hook",
                    elapsed_s=time.time() - start,
                )

            provider = get_active_provider(settings)
            api_key = get_api_key(settings, provider)
            model = hook.model or get_model(settings, provider)

            raw = _run_prompt_hook(
                hook.prompt or "", model, provider, api_key, context, hook.timeout
            )

        elif hook.hook_type == HookType.agent:
            if settings is None:
                return HookResult(
                    hook_name=hook.name,
                    event=context.hook_event_name,
                    decision="allow",
                    error="No settings available for agent hook",
                    elapsed_s=time.time() - start,
                )

            provider = get_active_provider(settings)
            api_key = get_api_key(settings, provider)
            model = hook.model or get_model(settings, provider)

            raw = _run_agent_hook(
                hook.prompt or "", model, provider, api_key, context, hook.timeout, tool_registry
            )

        else:
            raw = {"decision": "allow", "reason": f"Unknown hook type: {hook.hook_type}"}

    except Exception as e:
        raw = {"decision": "allow", "error": str(e)}

    elapsed = time.time() - start

    return HookResult(
        hook_name=hook.name,
        event=context.hook_event_name,
        decision=raw.get("decision", "allow"),
        reason=raw.get("reason", ""),
        modified_input=raw.get("modified_input"),
        additional_context=raw.get("additional_context", ""),
        system_message=raw.get("system_message", ""),
        output=raw.get("stdout", raw.get("error", "")),
        elapsed_s=elapsed,
        error=raw.get("error"),
    )


# ── Hook Registry ─────────────────────────────────────────────────────────────

class HookRegistry:
    """Discover, manage, and execute agent lifecycle hooks."""

    def __init__(self, sandbox_root: Path):
        self.sandbox_root = sandbox_root.resolve()
        self._hooks: List[HookConfig] = []
        self.diagnostics: List[str] = []

    def refresh(self) -> None:
        """Rescan project directories for hook configurations."""
        discovered: List[HookConfig] = []
        diagnostics: List[str] = []

        for search_dir in self._search_roots():
            if not search_dir.is_dir():
                continue
            for json_file in sorted(search_dir.glob("*.json")):
                try:
                    configs = self._parse_hook_file(json_file)
                    for cfg in configs:
                        cfg.source = str(json_file)
                    discovered.extend(configs)
                except Exception as exc:
                    diagnostics.append(f"{json_file}: could not load hooks ({exc})")

        self._hooks = sorted(discovered, key=lambda h: h.name)
        self.diagnostics = diagnostics

    def _search_roots(self) -> List[Path]:
        roots = []
        for relative_path in HOOK_SEARCH_DIRS:
            roots.append((self.sandbox_root / relative_path).resolve())
        return roots

    def _parse_hook_file(self, path: Path) -> List[HookConfig]:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)

        hooks_data = data.get("hooks", data)
        if isinstance(hooks_data, dict):
            all_hooks: List[HookConfig] = []
            for event_name, entries in hooks_data.items():
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict):
                            cfg = HookConfig(
                                name=entry.get("name", f"hook_{len(all_hooks)}"),
                                event=entry.get("event", event_name),
                                hook_type=entry.get("type", "command"),
                                matcher=entry.get("matcher"),
                                command=entry.get("command"),
                                prompt=entry.get("prompt"),
                                model=entry.get("model"),
                                timeout=entry.get("timeout", entry.get("timeoutSec", 30)),
                                decision_on_deny=entry.get("decision_on_deny", "block"),
                                source=str(path),
                            )
                            all_hooks.append(cfg)
            return all_hooks
        return []

    def list_hooks(self) -> List[HookConfig]:
        return list(self._hooks)

    def get_hooks_for_event(self, event: str) -> List[HookConfig]:
        return [h for h in self._hooks if h.event == event]

    def execute_hooks(
        self,
        event: str,
        context: HookEventContext,
        settings: Optional[Dict[str, Any]] = None,
        tool_registry: Optional[Dict[str, Callable]] = None,
        ui_callback: Optional[Callable[[HookResult], None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute all hooks registered for an event. For PreToolUse, returns
        a composite decision (most restrictive wins). For other events,
        returns collected context additions.
        """
        hooks = self.get_hooks_for_event(event)
        if not hooks:
            return {"decision": "allow", "results": [], "additional_context": ""}

        matching = [h for h in hooks if h.matches(context.tool_name)]
        if not matching:
            return {"decision": "allow", "results": [], "additional_context": ""}

        results: List[HookResult] = []
        decisions: List[str] = []
        context_additions: List[str] = []
        system_messages: List[str] = []
        modified_input: Optional[Dict[str, Any]] = None

        for hook in matching:
            result = _run_single_hook(hook, context, settings, tool_registry)
            results.append(result)

            if ui_callback:
                ui_callback(result)

            decisions.append(result.decision)
            if result.additional_context:
                context_additions.append(result.additional_context)
            if result.system_message:
                system_messages.append(result.system_message)
            if result.modified_input is not None:
                modified_input = result.modified_input

        # Most restrictive decision wins: block > ask > warn > allow
        final_decision = "allow"
        if "block" in decisions:
            final_decision = "block"
        elif "ask" in decisions:
            final_decision = "ask"
        elif "warn" in decisions:
            final_decision = "warn"

        return {
            "decision": final_decision,
            "results": results,
            "additional_context": "\n".join(context_additions),
            "system_messages": system_messages,
            "modified_input": modified_input,
        }

    def catalog_prompt(self) -> str:
        """Return the hooks catalog for the system prompt."""
        if not self._hooks:
            return ""

        lines = ["Active agent hooks:", ""]
        for hook in self._hooks:
            matcher_info = f" (matches: {hook.matcher})" if hook.matcher else ""
            lines.append(
                f"  - {hook.event}: {hook.name} [{hook.hook_type}]{matcher_info}"
            )

        return "\n".join(lines)

    def create_hook(
        self,
        name: str,
        event: str,
        hook_type: str = "command",
        matcher: Optional[str] = None,
        command: Optional[str] = None,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 30,
        decision_on_deny: str = "block",
    ) -> Dict[str, Any]:
        """Scaffold a new hook configuration file."""
        from .skills import normalize_skill_name

        slug = normalize_skill_name(name)
        if not slug:
            return {"error": "Invalid name. Use letters, numbers, hyphens, or underscores."}

        hooks_dir = (self.sandbox_root / ".indusbuilt" / "hooks").resolve()
        hooks_dir.mkdir(parents=True, exist_ok=True)

        hook_file = hooks_dir / f"{slug}.json"
        if hook_file.exists():
            return {"error": f"Hook config already exists at: {hook_file}"}

        hook_config = {
            "version": 1,
            "hooks": {
                event: [{
                    "name": slug,
                    "event": event,
                    "type": hook_type,
                }]
            },
        }

        entry = hook_config["hooks"][event][0]
        if matcher:
            entry["matcher"] = matcher
        if hook_type == "command":
            entry["command"] = command or "echo '{}'"
        elif hook_type in ("prompt", "agent"):
            entry["prompt"] = prompt or "Review the following and decide: $ARGUMENTS"
        if model:
            entry["model"] = model
        if timeout != 30:
            entry["timeout"] = timeout
        if decision_on_deny != "block":
            entry["decision_on_deny"] = decision_on_deny

        hook_file.write_text(
            json.dumps(hook_config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.refresh()
        return {"name": slug, "created": True, "path": str(hook_file)}
