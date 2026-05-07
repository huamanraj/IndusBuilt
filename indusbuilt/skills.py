"""
Agent Skills support for IndusBuilt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SKILL_SEARCH_DIRS = (
    ".agents/skills",
    ".indusbuilt/skills",
    "skills",
)

SKILL_FILE_NAME = "SKILL.md"
MAX_LISTED_RESOURCES = 50
SKIP_RESOURCE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


@dataclass(frozen=True)
class Skill:
    """Metadata for one discovered skill."""

    name: str
    description: str
    location: Path
    source: str

    @property
    def directory(self) -> Path:
        return self.location.parent


def normalize_skill_name(raw_name: str) -> str:
    """Normalize user input into a skill folder/name slug."""
    name = raw_name.strip().lower()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^a-z0-9_-]", "", name)
    name = re.sub(r"[-_]{2,}", "-", name)
    return name.strip("-_")


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_frontmatter_value(lines: List[str], start_index: int, value: str) -> Tuple[str, int]:
    """Parse a simple YAML scalar, including block scalar values."""
    if value not in ("|", ">"):
        return _strip_quotes(value.strip()), start_index + 1

    collected: List[str] = []
    index = start_index + 1
    while index < len(lines):
        line = lines[index]
        if line and not line.startswith((" ", "\t")):
            break
        collected.append(line.strip())
        index += 1

    separator = "\n" if value == "|" else " "
    return separator.join(part for part in collected if part), index


def _parse_frontmatter(frontmatter: str) -> Dict[str, str]:
    """Parse the top-level YAML fields used by SKILL.md without extra deps."""
    metadata: Dict[str, str] = {}
    lines = frontmatter.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            index += 1
            continue

        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            index += 1
            continue

        value, index = _parse_frontmatter_value(lines, index, raw_value.strip())
        metadata[key] = value

    return metadata


def parse_skill_file(skill_file: Path) -> Tuple[Optional[Skill], str, List[str]]:
    """Parse one SKILL.md file and return metadata, body, and diagnostics."""
    diagnostics: List[str] = []

    try:
        text = skill_file.read_text(encoding="utf-8")
    except Exception as exc:
        return None, "", [f"{skill_file}: could not read skill file ({exc})"]

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "", [f"{skill_file}: missing YAML frontmatter"]

    closing_index: Optional[int] = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break

    if closing_index is None:
        return None, "", [f"{skill_file}: missing closing frontmatter delimiter"]

    metadata = _parse_frontmatter("\n".join(lines[1:closing_index]))
    name = metadata.get("name", "").strip()
    description = metadata.get("description", "").strip()

    if not name:
        name = skill_file.parent.name
        diagnostics.append(f"{skill_file}: missing name; using directory name '{name}'")

    if not description:
        return None, "", [f"{skill_file}: missing required description"]

    if name != skill_file.parent.name:
        diagnostics.append(
            f"{skill_file}: name '{name}' does not match directory '{skill_file.parent.name}'"
        )

    if len(name) > 64:
        diagnostics.append(f"{skill_file}: name exceeds 64 characters")

    body = "\n".join(lines[closing_index + 1 :]).strip()
    skill = Skill(
        name=name,
        description=description,
        location=skill_file.resolve(),
        source=str(skill_file.parent.parent.resolve()),
    )
    return skill, body, diagnostics


class SkillRegistry:
    """Discover, activate, and create project-level Agent Skills."""

    def __init__(self, sandbox_root: Path):
        self.sandbox_root = sandbox_root.resolve()
        self.skills: Dict[str, Skill] = {}
        self.diagnostics: List[str] = []
        self._active_skill_blocks: Dict[str, str] = {}

    def refresh(self) -> None:
        """Rescan project skill locations."""
        discovered: Dict[str, Skill] = {}
        diagnostics: List[str] = []

        for search_root in self._search_roots():
            if not search_root.is_dir():
                continue

            for skill_file in self._skill_files(search_root):
                skill, _body, skill_diagnostics = parse_skill_file(skill_file)
                diagnostics.extend(skill_diagnostics)
                if skill is None:
                    continue

                if skill.name in discovered:
                    diagnostics.append(
                        f"{skill.location}: shadows earlier skill named '{skill.name}'"
                    )
                discovered[skill.name] = skill

        self.skills = dict(sorted(discovered.items()))
        self.diagnostics = diagnostics
        self._active_skill_blocks = {
            name: block for name, block in self._active_skill_blocks.items() if name in self.skills
        }

    def _search_roots(self) -> Iterable[Path]:
        for relative_path in SKILL_SEARCH_DIRS:
            yield (self.sandbox_root / relative_path).resolve()

    def _skill_files(self, search_root: Path) -> Iterable[Path]:
        for child in sorted(search_root.iterdir()):
            if child.is_dir():
                skill_file = child / SKILL_FILE_NAME
                if skill_file.is_file():
                    yield skill_file

    def list_skills(self) -> List[Skill]:
        return list(self.skills.values())

    def catalog_prompt(self) -> str:
        """Return the compact skills catalog for the system prompt."""
        if not self.skills:
            return ""

        entries = []
        for skill in self.list_skills():
            entries.append(
                "  <skill>\n"
                f"    <name>{escape(skill.name)}</name>\n"
                f"    <description>{escape(skill.description)}</description>\n"
                f"    <location>{escape(str(skill.location))}</location>\n"
                "  </skill>"
            )

        return (
            "The following skills provide specialized instructions for specific tasks. "
            "When a task matches a skill's description, call activate_skill with the "
            "skill name before proceeding. When a skill references relative paths, "
            "resolve them against that skill's directory.\n"
            "<available_skills>\n"
            + "\n".join(entries)
            + "\n</available_skills>"
        )

    def active_prompt(self) -> str:
        """Return active skill instructions for durable model context."""
        if not self._active_skill_blocks:
            return ""
        return "Activated skill instructions:\n" + "\n\n".join(self._active_skill_blocks.values())

    def activation_tool_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "activate_skill",
                "description": "Load the full instructions for an available Agent Skill by name.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": list(self.skills.keys()),
                            "description": "The skill name from the available skills catalog.",
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    def activate(self, name: str) -> Dict[str, Any]:
        skill = self.skills.get(name)
        if skill is None:
            return {
                "error": f"Unknown skill: {name}",
                "available_skills": list(self.skills.keys()),
            }

        if name in self._active_skill_blocks:
            return {
                "name": name,
                "activated": True,
                "already_active": True,
                "message": "Skill instructions were already loaded in context.",
            }

        block, resource_count = self._format_skill_content(skill)
        self._active_skill_blocks[name] = block
        return {
            "name": name,
            "activated": True,
            "already_active": False,
            "skill_directory": str(skill.directory),
            "resource_count": resource_count,
            "message": "Skill instructions are now loaded in context.",
        }

    def _format_skill_content(self, skill: Skill) -> Tuple[str, int]:
        _metadata, body, diagnostics = parse_skill_file(skill.location)
        if diagnostics:
            self.diagnostics.extend(diagnostics)

        resources = self._resource_paths(skill.directory)
        resource_lines = "\n".join(f"  <file>{escape(path)}</file>" for path in resources)
        if resource_lines:
            resources_block = f"\n<skill_resources>\n{resource_lines}\n</skill_resources>"
        else:
            resources_block = "\n<skill_resources />"

        block = (
            f'<skill_content name="{escape(skill.name)}">\n'
            f"{body}\n\n"
            f"Skill directory: {escape(str(skill.directory))}\n"
            "Relative paths in this skill are relative to the skill directory."
            f"{resources_block}\n"
            "</skill_content>"
        )
        return block, len(resources)

    def _resource_paths(self, skill_directory: Path) -> List[str]:
        resources: List[str] = []
        for path in sorted(skill_directory.rglob("*")):
            if not path.is_file() or path.name == SKILL_FILE_NAME:
                continue
            if any(part in SKIP_RESOURCE_DIRS for part in path.relative_to(skill_directory).parts):
                continue
            resources.append(path.relative_to(skill_directory).as_posix())
            if len(resources) >= MAX_LISTED_RESOURCES:
                break
        return resources

    def create_skill(self, raw_name: str, description: str) -> Dict[str, Any]:
        skill_name = normalize_skill_name(raw_name)
        clean_description = description.strip()

        if not skill_name:
            return {"error": "Skill name must contain letters, numbers, hyphens, or underscores."}
        if not clean_description:
            return {"error": "Skill description is required."}

        skill_dir = (self.sandbox_root / "skills" / skill_name).resolve()
        try:
            skill_dir.relative_to(self.sandbox_root)
        except ValueError:
            return {"error": "Skill path would escape the sandbox."}

        skill_file = skill_dir / SKILL_FILE_NAME
        if skill_file.exists():
            return {"error": f"Skill already exists: {skill_file}"}

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(_skill_template(skill_name, clean_description), encoding="utf-8")
        self.refresh()

        return {
            "name": skill_name,
            "created": True,
            "path": str(skill_file),
        }


def _skill_template(name: str, description: str) -> str:
    title = name.replace("-", " ").replace("_", " ").title()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {title}\n\n"
        "Use this skill when the task matches the description above.\n\n"
        "## Instructions\n\n"
        "- Add the workflow, commands, checks, or project-specific guidance this skill should apply.\n"
    )
