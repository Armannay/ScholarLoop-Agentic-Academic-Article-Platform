"""Lazy skill loader: index-only reads, deterministic selection, hard budgets."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.enums import AgentRole
from app.contracts.task import Task
from app.skills.loader import (
    MAX_SKILLS_PER_TASK,
    STARTER_SKILL_NAMES,
    SkillBodyTooLarge,
    SkillLoader,
    SkillMetadataError,
    SkillNotFound,
)
from tests.factories import sample_ulid

pytestmark = pytest.mark.skills


def write_skill(
    root: Path,
    name: str,
    *,
    body: str | None = None,
    description: str = "A skill used by the loader tests.",
    roles: tuple[str, ...] = ("writer",),
    max_tokens: int = 400,
    triggers: tuple[str, ...] = ("draft",),
    name_override: str | None = None,
    omit: tuple[str, ...] = (),
) -> Path:
    """Write one SKILL.md with valid front matter unless ``omit`` removes a key."""
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name_override or name}"]
    if "description" not in omit:
        lines.append(f'description: "{description}"')
    if "roles" not in omit:
        lines.append(f"roles: [{', '.join(roles)}]")
    if "max_tokens" not in omit:
        lines.append(f"max_tokens: {max_tokens}")
    if "triggers" not in omit:
        lines.append(f"triggers: [{', '.join(triggers)}]")
    lines.append("---")
    content = body if body is not None else "## Instructions\nUse this skill when drafting."
    path.write_text("\n".join(lines) + "\n" + content + "\n", encoding="utf-8")
    return path


def make_task(
    *,
    title: str = "Draft the related work section",
    instructions: str = "Build a literature matrix and outline the related work.",
    role: AgentRole = AgentRole.WRITER,
    section_ids: tuple[str, ...] = ("related-work",),
) -> Task:
    return Task(
        id=sample_ulid(1),
        title=title,
        role=role,
        section_ids=list(section_ids),
        instructions=instructions,
        acceptance=["Addresses the task"],
        token_budget=50_000,
    )


# --------------------------------------------------------------------------- index
def test_index_returns_names_and_descriptions_only(tmp_path: Path) -> None:
    """The index is the compact, cacheable prefix - no bodies, no roles, no triggers."""
    write_skill(tmp_path, "alpha", description="Alpha does the first thing.")
    write_skill(tmp_path, "beta", description="Beta does the second thing.")
    loader = SkillLoader(tmp_path)

    entries = loader.index()

    assert [entry.name for entry in entries] == ["alpha", "beta"]
    assert [entry.description for entry in entries] == [
        "Alpha does the first thing.",
        "Beta does the second thing.",
    ]
    assert entries[0].__dict__.keys() == {"name", "description"}
    assert loader.render_index().startswith("- alpha: Alpha does the first thing.")
    assert loader.loaded_names() == ()


def test_loading_is_lazy_and_only_selected_bodies_are_read(tmp_path: Path) -> None:
    """``index`` reads front matter; ``select`` reads exactly the chosen bodies."""
    write_skill(tmp_path, "alpha", triggers=("draft",))
    write_skill(tmp_path, "beta", triggers=("draft",))
    write_skill(tmp_path, "gamma", triggers=("draft",))
    loader = SkillLoader(tmp_path)

    assert loader.index()
    assert loader.loaded_names() == ()

    selection = loader.select(make_task())
    assert len(selection.skills) == 2
    assert loader.loaded_names() == selection.names
    assert "gamma" not in loader.loaded_names()


# --------------------------------------------------------------------------- selection
def test_selection_is_deterministic_and_never_exceeds_two(tmp_path: Path) -> None:
    """Equal scores break ties by name; the hard cap is two regardless of candidates."""
    for name in ("charlie", "alpha", "bravo"):
        write_skill(tmp_path, name, triggers=("draft",))
    loader = SkillLoader(tmp_path)
    task = make_task()

    first = loader.select(task)
    second = loader.select(task)

    assert first.names == second.names == ("alpha", "bravo")
    assert len(first.skills) == MAX_SKILLS_PER_TASK
    with pytest.raises(ValueError):
        loader.select(task, max_skills=MAX_SKILLS_PER_TASK + 1)


def test_selection_requires_a_trigger_match(tmp_path: Path) -> None:
    """No trigger, no skill: selection is evidence-driven, not role-defaulted."""
    write_skill(tmp_path, "alpha", triggers=("conclusion", "abstract"))
    loader = SkillLoader(tmp_path)
    selection = loader.select(make_task())
    assert selection.names == ()
    assert selection.render() == ""


def test_selection_respects_role_permissions(tmp_path: Path) -> None:
    """A skill whose roles exclude the task role is never a candidate."""
    write_skill(tmp_path, "review-only", roles=("reviewer",), triggers=("draft",))
    loader = SkillLoader(tmp_path)
    assert loader.select(make_task(role=AgentRole.WRITER)).names == ()
    assert loader.select(make_task(role=AgentRole.REVIEWER)).names == ("review-only",)


def test_selection_respects_the_token_budget(tmp_path: Path) -> None:
    """A skill that does not fit the remaining budget is skipped, not squeezed in."""
    long_body = "word " * 60  # 300 chars / 4 => ~75 tokens
    write_skill(tmp_path, "alpha", body=long_body, triggers=("draft",))
    write_skill(tmp_path, "bravo", body=long_body, triggers=("draft",))
    loader = SkillLoader(tmp_path)

    selection = loader.select(make_task(), token_budget=100)

    assert selection.names == ("alpha",)
    assert selection.tokens <= 100
    assert selection.budget == 100


def test_selection_honours_the_triggers_it_reports(tmp_path: Path) -> None:
    """The selection explains itself: matched triggers are returned per skill."""
    write_skill(
        tmp_path,
        "alpha",
        triggers=("draft", "related work", "literature"),
    )
    loader = SkillLoader(tmp_path)
    selection = loader.select(make_task())
    assert selection.names == ("alpha",)
    assert set(selection.triggers_for("alpha")) == {"draft", "related work", "literature"}


# --------------------------------------------------------------------------- validation
def test_missing_required_front_matter_key_is_reported(tmp_path: Path) -> None:
    """Malformed metadata names the file and the missing key."""
    path = write_skill(tmp_path, "alpha", omit=("max_tokens",))
    loader = SkillLoader(tmp_path)
    with pytest.raises(SkillMetadataError) as caught:
        loader.index()
    assert str(path) in str(caught.value)
    assert "max_tokens" in str(caught.value)


def test_description_over_twenty_words_is_rejected(tmp_path: Path) -> None:
    """The description travels in the index, so it is capped at 20 words."""
    write_skill(
        tmp_path,
        "alpha",
        description=" ".join(f"word{index}" for index in range(21)),
    )
    with pytest.raises(SkillMetadataError) as caught:
        SkillLoader(tmp_path).index()
    assert "at most 20 words" in str(caught.value)


def test_unknown_role_is_rejected(tmp_path: Path) -> None:
    """Roles are the closed AgentRole vocabulary, never free text."""
    write_skill(tmp_path, "alpha", roles=("wizard",))
    with pytest.raises(SkillMetadataError):
        SkillLoader(tmp_path).index()


def test_directory_name_must_match_the_front_matter(tmp_path: Path) -> None:
    """Two names for one skill would make selection ambiguous."""
    write_skill(tmp_path, "alpha", name_override="beta")
    with pytest.raises(SkillMetadataError) as caught:
        SkillLoader(tmp_path).index()
    assert "does not match directory" in str(caught.value)


def test_unterminated_front_matter_is_reported(tmp_path: Path) -> None:
    """A broken header fails loudly instead of reading the whole file as metadata."""
    path = tmp_path / "alpha" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: alpha\ndescription: broken\n", encoding="utf-8")
    with pytest.raises(SkillMetadataError):
        SkillLoader(tmp_path).index()


def test_body_over_declared_max_tokens_is_refused(tmp_path: Path) -> None:
    """A skill that lies about its size cannot be selected into a prompt."""
    write_skill(tmp_path, "alpha", body="word " * 400, max_tokens=10, triggers=("draft",))
    loader = SkillLoader(tmp_path)
    with pytest.raises(SkillBodyTooLarge):
        loader.load("alpha")
    with pytest.raises(SkillBodyTooLarge):
        loader.select(make_task())


def test_unknown_skill_is_a_typed_error(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha")
    with pytest.raises(SkillNotFound):
        SkillLoader(tmp_path).load("missing")


# --------------------------------------------------------------------------- shipped skills
def test_all_seven_starter_skills_are_installed(repo_root: Path) -> None:
    loader = SkillLoader(repo_root / "skills")
    assert set(loader.names()) == set(STARTER_SKILL_NAMES)
    assert len(loader.names()) == 7


def test_every_shipped_skill_is_within_the_four_hundred_token_budget(
    repo_root: Path,
) -> None:
    loader = SkillLoader(repo_root / "skills")
    inspection = loader.inspect()
    assert inspection.problems == ()
    for audit in inspection.audits:
        assert audit.tokens <= 400, f"{audit.name} is {audit.tokens} tokens"
        assert audit.tokens <= audit.declared_max_tokens


def test_shipped_skill_bodies_are_useful_and_not_placeholder_text(
    repo_root: Path,
) -> None:
    """A skill that says nothing costs tokens and teaches nothing."""
    loader = SkillLoader(repo_root / "skills")
    for name in STARTER_SKILL_NAMES:
        skill = loader.load(name)
        assert skill.body.startswith("##")
        assert len(skill.body) > 300
        assert "TODO:" not in skill.body
        assert "lorem ipsum" not in skill.body.lower()
