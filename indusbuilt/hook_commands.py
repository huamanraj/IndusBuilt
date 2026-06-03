"""
Slash command handlers for Agent Hooks.
"""
from __future__ import annotations

from .hooks import HookRegistry, HookEvent, HookType
from .ui import choose_from_list, print_error, print_success


class HookCommandHandler:
    """CLI command facade for listing, inspecting, and creating hooks."""

    def __init__(self, registry: HookRegistry):
        self.registry = registry

    def show(self) -> None:
        self.registry.refresh()
        hooks = self.registry.list_hooks()
        if not hooks:
            print_error("No hooks configured. Create one with /create hook.")
            return

        print("Active hooks:")
        for hook in hooks:
            matcher = f" (matches: {hook.matcher})" if hook.matcher else ""
            print(f"  - [{hook.event}] {hook.name} :: {hook.hook_type}{matcher}")
            print(f"    source: {hook.source}")

        if self.registry.diagnostics:
            print("\nHook diagnostics:")
            for diagnostic in self.registry.diagnostics:
                print(f"  - {diagnostic}")

    def show_detail(self, name: str) -> None:
        self.registry.refresh()
        for hook in self.registry.list_hooks():
            if hook.name == name:
                print(f"Hook: {hook.name}")
                print(f"  Event:      {hook.event}")
                print(f"  Type:       {hook.hook_type}")
                print(f"  Matcher:    {hook.matcher or '(all)'}")
                print(f"  Timeout:   {hook.timeout}s")
                print(f"  Deny mode:  {hook.decision_on_deny}")
                if hook.command:
                    print(f"  Command:    {hook.command[:200]}")
                if hook.prompt:
                    print(f"  Prompt:     {hook.prompt[:200]}")
                if hook.model:
                    print(f"  Model:      {hook.model}")
                print(f"  Source:     {hook.source}")
                return
        print_error(f"Hook not found: {name}")

    def handle_hooks_command(self, command: str) -> None:
        parts = command.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        arg_lower = arg.lower()

        if arg_lower in ("list", "ls", ""):
            self.show()
            return

        if arg_lower == "refresh":
            self.registry.refresh()
            print_success("Hooks refreshed.")
            self.show()
            return

        self.show_detail(arg)

    def handle_create_hook_command(self, command: str) -> None:
        lowered = command.lower()
        prefix = "/create hook"
        if lowered.startswith(prefix):
            name = command[len(prefix):].strip()
        else:
            name = ""

        if not name:
            name = input("Hook name: ").strip()
            if not name:
                print_error("Hook name is required.")
                return

        print("\nHook events:")
        events = [e.value for e in HookEvent]
        for i, evt in enumerate(events):
            print(f"  {i + 1}. {evt}")
        event_idx = input(f"Select event (1-{len(events)}): ").strip()
        try:
            event = events[int(event_idx) - 1]
        except (ValueError, IndexError):
            print_error("Invalid selection.")
            return

        print("\nHook types:")
        types = [t.value for t in HookType]
        for i, ht in enumerate(types):
            print(f"  {i + 1}. {ht}")
        type_idx = input(f"Select type (1-{len(types)}): ").strip()
        try:
            hook_type = types[int(type_idx) - 1]
        except (ValueError, IndexError):
            print_error("Invalid selection.")
            return

        matcher = input("Matcher regex (enter for all tools): ").strip() or None

        command_str = None
        prompt_str = None
        model_str = None

        if hook_type == "command":
            command_str = input("Shell command: ").strip()
            if not command_str:
                print_error("Command is required for command hooks.")
                return
        elif hook_type in ("prompt", "agent"):
            prompt_str = input("Prompt (use $ARGUMENTS for context): ").strip()
            if not prompt_str:
                print_error("Prompt is required.")
                return
            model_str = input("Model (enter for default): ").strip() or None

        timeout_str = input("Timeout in seconds (default 30): ").strip()
        timeout = int(timeout_str) if timeout_str else 30

        deny_mode = input("On deny: block | warn | ask (default block): ").strip() or "block"

        result = self.registry.create_hook(
            name=name,
            event=event,
            hook_type=hook_type,
            matcher=matcher,
            command=command_str,
            prompt=prompt_str,
            model=model_str,
            timeout=timeout,
            decision_on_deny=deny_mode,
        )

        if "error" in result:
            print_error(result["error"])
            return

        print_success(f"Created hook '{result['name']}' at {result['path']}")
