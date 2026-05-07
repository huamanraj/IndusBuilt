"""
Slash command handlers for Agent Skills.
"""
from __future__ import annotations

from .skills import SkillRegistry, normalize_skill_name
from .ui import choose_from_list, print_error, print_success


class SkillCommandHandler:
    """CLI command facade for listing, loading, and creating skills."""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def activate_by_name(self, name: str) -> None:
        self.registry.refresh()
        skill_name = name if name in self.registry.skills else normalize_skill_name(name)
        result = self.registry.activate(skill_name)
        if "error" in result:
            print_error(result["error"])
            available = result.get("available_skills") or []
            if available:
                print("Available skills: " + ", ".join(available))
            return

        if result.get("already_active"):
            print_success(f"Skill '{skill_name}' is already loaded.")
        else:
            print_success(f"Loaded skill '{skill_name}' into the agent context.")

    def show(self) -> None:
        self.registry.refresh()
        skills = self.registry.list_skills()
        if not skills:
            print_error("No skills found. Create one with /create skills.")
            return

        print("Available skills:")
        for skill in skills:
            print(f"  - {skill.name}: {skill.description}")

        if self.registry.diagnostics:
            print("\nSkill diagnostics:")
            for diagnostic in self.registry.diagnostics:
                print(f"  - {diagnostic}")

    def handle_skills_command(self, command: str) -> None:
        parts = command.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        arg_lower = arg.lower()

        if arg_lower in ("list", "ls"):
            self.show()
            return

        if arg_lower == "refresh":
            self.registry.refresh()
            print_success("Skills refreshed.")
            self.show()
            return

        if arg:
            self.activate_by_name(arg)
            return

        self.registry.refresh()
        skills = self.registry.list_skills()
        if not skills:
            print_error("No skills found. Create one with /create skills.")
            return

        options = [f"{skill.name} - {skill.description}" for skill in skills]
        options.extend(["Refresh skills", "Cancel"])
        selected = choose_from_list("Available Skills", options)
        if selected < len(skills):
            self.activate_by_name(skills[selected].name)
        elif selected == len(skills):
            self.registry.refresh()
            print_success("Skills refreshed.")

    def handle_create_skill_command(self, command: str) -> None:
        lowered = command.lower()
        prefixes = ("/create skills", "/create skill")
        skill_name = ""
        for prefix in prefixes:
            if lowered.startswith(prefix):
                skill_name = command[len(prefix):].strip()
                break

        if not skill_name:
            skill_name = input("Skill name: ").strip()

        description = input("Skill description: ").strip()
        result = self.registry.create_skill(skill_name, description)
        if "error" in result:
            print_error(result["error"])
            return

        print_success(f"Created skill '{result['name']}' at {result['path']}.")
