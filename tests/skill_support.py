"""Read the installed skill's core and on-demand references for prose contracts."""

from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "SKILL.md"


def skill_references(pattern: str = "*.md") -> list[Path]:
    return sorted((SKILL.parent / "references").glob(pattern))


def skill_text(*relative_paths: str) -> str:
    paths = (
        [SKILL.parent / name for name in relative_paths]
        if relative_paths
        else [SKILL, *skill_references()]
    )
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)
