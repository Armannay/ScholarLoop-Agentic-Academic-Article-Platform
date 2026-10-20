"""Lazy skills: compact index, deterministic selection, bounded bodies.

Skills are prompt assets stored as ``skills/<name>/SKILL.md`` with YAML front matter.
The loader is the only component allowed to read them (an agent never loads a skill
on its own), which is what keeps selection auditable and the prompt cacheable.
"""

from __future__ import annotations

from app.skills.loader import (
    DEFAULT_SKILL_BUDGET_TOKENS,
    DEFAULT_SKILLS_DIR,
    MAX_DESCRIPTION_WORDS,
    MAX_SKILL_TOKENS,
    MAX_SKILLS_PER_TASK,
    STARTER_SKILL_NAMES,
    STARTER_SKILL_TOKEN_CEILING,
    Skill,
    SkillAudit,
    SkillBodyTooLarge,
    SkillError,
    SkillIndexEntry,
    SkillInspection,
    SkillLoader,
    SkillMeta,
    SkillMetadataError,
    SkillNotFound,
    SkillProblem,
    SkillSelection,
    default_skills_dir,
    lint_skills,
)

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "DEFAULT_SKILL_BUDGET_TOKENS",
    "MAX_DESCRIPTION_WORDS",
    "MAX_SKILLS_PER_TASK",
    "MAX_SKILL_TOKENS",
    "STARTER_SKILL_NAMES",
    "STARTER_SKILL_TOKEN_CEILING",
    "Skill",
    "SkillAudit",
    "SkillBodyTooLarge",
    "SkillError",
    "SkillIndexEntry",
    "SkillInspection",
    "SkillLoader",
    "SkillMeta",
    "SkillMetadataError",
    "SkillNotFound",
    "SkillProblem",
    "SkillSelection",
    "default_skills_dir",
    "lint_skills",
]
