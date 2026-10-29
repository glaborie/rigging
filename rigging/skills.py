"""Skills: markdown packs, progressive disclosure (ADR-0006).

Only name + description enter the system prompt. The body is fetched by the
`load_skill` tool. This is why 20 installed skills cost ~1k tokens instead of 60k.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from rigging.protocols import SkillMeta


class MarkdownSkill:
    __slots__ = ("body", "meta")

    def __init__(self, meta: SkillMeta, body: str) -> None:
        self.meta, self.body = meta, body

    def __repr__(self) -> str:
        return f"<Skill {self.meta.name}>"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return (yaml.safe_load(parts[1]) or {}), parts[2].lstrip("\n")


class FilesystemSkillResolver:
    """Discovers `<dir>/<name>/SKILL.md` and `<dir>/<name>.md`."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._cache: dict[str, MarkdownSkill] = {}

    def _paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        found = sorted(self.directory.glob("*/SKILL.md")) + sorted(self.directory.glob("*.md"))
        return found

    def discover(self) -> list[SkillMeta]:
        metas: list[SkillMeta] = []
        for path in self._paths():
            meta, _body = _split_frontmatter(path.read_text(encoding="utf-8"))
            name = meta.get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem)
            metas.append(SkillMeta(name=str(name), description=str(meta.get("description", "")),
                                   path=str(path)))
        return metas

    def load(self, name: str) -> MarkdownSkill:
        if name in self._cache:
            return self._cache[name]
        for meta in self.discover():
            if meta.name == name:
                _m, body = _split_frontmatter(Path(meta.path).read_text(encoding="utf-8"))
                skill = MarkdownSkill(meta, body)
                self._cache[name] = skill
                return skill
        raise KeyError(name)

    def index_block(self, only: list[str] | None = None) -> str:
        """The index that goes into the system prompt: names and one-line descriptions."""
        metas = [m for m in self.discover() if only is None or m.name in only]
        if not metas:
            return ""
        lines = ["<skills>", "Load one with the load_skill tool when it applies."]
        lines += [f"- {m.name}: {m.description}" for m in metas]
        lines.append("</skills>")
        return "\n".join(lines)
