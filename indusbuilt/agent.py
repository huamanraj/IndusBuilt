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
from .settings import (
    MODEL_CHOICES,
    PROVIDERS,
    get_active_provider,
    get_api_key,
    get_model,
    save_settings,
    set_active_provider,
    set_api_key,
    set_model,
)
from .skill_commands import SkillCommandHandler
from .skills import SkillRegistry
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
def make_tools(sandbox_root: Path, skill_registry: Optional[SkillRegistry] = None):
    """Returns the core tools bound to a sandbox_root."""

    def read_file_tool(filename: str) -> Dict[str, Any]:
        """
        Gets the full content of a file provided by the user.
        Only files within the current working (sandbox) directory are accessible.

        :param filename: The name or relative path of the file to read.
        :return: The full content of the file.
        """
        try:
            full_path = resolve_sandboxed_path(filename, sandbox_root)
            content = full_path.read_text(encoding="utf-8")
            return {"file_path": str(full_path), "content": content}
        except ValueError as e:
            return {"error": str(e)}
        except FileNotFoundError:
            return {"error": f"File not found: {filename}"}
        except Exception as e:
            return {"error": str(e)}

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

    def edit_file_tool(path: str, old_str: str, new_str: str) -> Dict[str, Any]:
        """
        Creates or edits a file within the sandbox directory.
        - If old_str is empty, creates/overwrites the file with new_str.
        - Otherwise, replaces the first occurrence of old_str with new_str.
        Only files within the current working (sandbox) directory can be edited.

        :param path: The relative path of the file to edit or create.
        :param old_str: The string to replace. Use empty string '' to create/overwrite the file.
        :param new_str: The new string to replace old_str with, or full content when creating.
        :return: A dict with the file path and action taken.
        """
        try:
            full_path = resolve_sandboxed_path(path, sandbox_root)
            # Ensure parent directories exist
            full_path.parent.mkdir(parents=True, exist_ok=True)

            if old_str == "":
                full_path.write_text(new_str, encoding="utf-8")
                return {"path": str(full_path), "action": "created_file"}

            if not full_path.exists():
                return {"error": f"File not found: {path}"}

            original = full_path.read_text(encoding="utf-8")
            if old_str not in original:
                return {"path": str(full_path), "action": "old_str_not_found"}

            edited = original.replace(old_str, new_str, 1)
            full_path.write_text(edited, encoding="utf-8")
            return {"path": str(full_path), "action": "edited"}
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    tools = {
        "read_file":  read_file_tool,
        "list_files": list_files_tool,
        "edit_file":  edit_file_tool,
    }

    if skill_registry is not None:
        tools["activate_skill"] = skill_registry.activate

    return tools


# ── OpenAI tool schemas ───────────────────────────────────────────────────────
CORE_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Gets the full content of a file. Only files within the sandbox (launch directory) are accessible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "The relative path of the file to read."
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lists files and directories inside a path. Only paths within the sandbox are accessible.",
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
            "name": "edit_file",
            "description": "Creates or edits a file. Pass old_str='' to create/overwrite. Otherwise replaces first occurrence of old_str with new_str. Only operates within the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file to create or edit."
                    },
                    "old_str": {
                        "type": "string",
                        "description": "String to find and replace. Use empty string to create/overwrite the file."
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement string or full file content when creating."
                    }
                },
                "required": ["path", "old_str", "new_str"]
            }
        }
    }
]


def build_openai_tools(skill_registry: Optional[SkillRegistry] = None) -> List[Dict[str, Any]]:
    tools = list(CORE_OPENAI_TOOLS)
    if skill_registry is not None and skill_registry.skills:
        tools.append(skill_registry.activation_tool_schema())
    return tools


# ── System prompt ─────────────────────────────────────────────────────────────
def build_system_prompt(sandbox_root: Path, skill_registry: Optional[SkillRegistry] = None) -> str:
    prompt = f"""You are IndusBuilt, an expert coding assistant.

Sandbox directory: {sandbox_root}

IMPORTANT RULES:
- You can ONLY read, list, or edit files INSIDE the sandbox directory above.
- Never attempt to access files outside of the sandbox.
- When creating code, follow best practices and write clean, documented code.
- Always read a file before editing it unless you are creating a new one.
- Chain tool calls as needed to complete tasks (read → understand → edit).
- When done, give a clear summary of what you changed.

You have these tools available:
1. read_file  – read the contents of any file in the sandbox
2. list_files – list directory contents
3. edit_file  – create new files or patch existing ones
"""

    if skill_registry is not None:
        catalog = skill_registry.catalog_prompt()
        active_skills = skill_registry.active_prompt()
        if catalog:
            prompt += (
                "4. activate_skill – load skill instructions when available skills match the task\n"
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
    tool_registry = make_tools(sandbox_root, skill_registry)
    conversation: List[Dict] = []
    active_provider = get_active_provider(settings)
    active_model = get_model(settings, active_provider)

    print_startup_banner(sandbox_root=sandbox_root, model=active_model)
    print_runtime_meta(provider=active_provider, model=active_model)

    def refresh_runtime_state() -> Dict[str, str]:
        provider = get_active_provider(settings)
        model = get_model(settings, provider)
        api_key = _get_effective_api_key(settings, provider)
        return {
            "provider": provider,
            "model": model,
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
                    "Show current settings",
                    "Skills",
                    "Create skill",
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
                state = refresh_runtime_state()
                print_runtime_meta(provider=state["provider"], model=state["model"])
            elif selected == 4:
                skill_commands.handle_skills_command("/skills")
            elif selected == 5:
                skill_commands.handle_create_skill_command("/create skills")
            elif selected == 6:
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

        if raw.startswith("/create skill"):
            skill_commands.handle_create_skill_command(raw_command)
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
                messages=[
                    {"role": "system", "content": build_system_prompt(sandbox_root, skill_registry)}
                ] + conversation,
                tools=build_openai_tools(skill_registry),
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
                break

            # There are tool calls to execute
            # Add assistant message (with tool_calls) to conversation
            conversation.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
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

                print_tool_call(tool_name=tool_name, args=args, result=result)

                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False),
                })
