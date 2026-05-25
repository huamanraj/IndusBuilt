"""
Slash command handlers for SubAgents.
"""
from __future__ import annotations

from .subagents import SubAgentRegistry
from .ui import choose_from_list, print_error, print_success


class SubAgentCommandHandler:
    """CLI command facade for listing, inspecting, and creating subagents."""

    def __init__(self, registry: SubAgentRegistry):
        self.registry = registry

    def show(self) -> None:
        self.registry.refresh()
        agents = self.registry.list_agents()
        if not agents:
            print_error("No subagents found.")
            return

        builtin = [a for a in agents if a.source == "builtin"]
        custom = [a for a in agents if a.source != "builtin"]

        print("Available subagents:\n")
        if builtin:
            print("  Built-in:")
            for sa in builtin:
                print(f"    {sa.name}")
                print(f"      {sa.description}")
        if custom:
            print("\n  Custom:")
            for sa in custom:
                print(f"    {sa.name}")
                print(f"      {sa.description}")
                print(f"      source: {sa.source}")
        print()

    def show_detail(self, name: str) -> None:
        self.registry.refresh()
        sa = self.registry.get(name)
        if not sa:
            available = [a.name for a in self.registry.list_agents()]
            print_error(f"Unknown subagent: '{name}'")
            if available:
                print(f"  Available: {', '.join(available)}")
            return

        print(f"\nSubAgent: {sa.name}")
        print(f"  Description : {sa.description}")
        print(f"  Tools       : {', '.join(sa.tools)}")
        print(f"  Max turns   : {sa.max_turns}")
        source_label = "builtin" if sa.source == "builtin" else sa.source
        print(f"  Source      : {source_label}")
        print()

    def handle_subagents_command(self, command: str) -> None:
        parts = command.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg or arg.lower() in ("list", "ls"):
            self.show()
            return

        if arg.lower() == "refresh":
            self.registry.refresh()
            print_success("SubAgents refreshed.")
            self.show()
            return

        self.show_detail(arg)

    def handle_create_subagent_command(self, command: str) -> None:
        parts = command.strip().split()
        name_from_cmd = ""
        for i, part in enumerate(parts):
            if part.lower() == "subagent" and i + 1 < len(parts):
                name_from_cmd = parts[i + 1]
                break

        name = name_from_cmd or input("SubAgent name: ").strip()
        if not name:
            print_error("Name is required.")
            return

        description = input("When should the main agent use this subagent? ").strip()
        if not description:
            print_error("Description is required.")
            return

        purpose = input("What does this subagent do? ").strip()
        if not purpose:
            print_error("Purpose is required.")
            return

        result = self.registry.create_custom(name, description, purpose)
        if "error" in result:
            print_error(result["error"])
            return

        print_success(f"Created subagent '{result['name']}' at {result['path']}.")
        print(f"Edit the AGENT.md file to customize its instructions and tool access.")
