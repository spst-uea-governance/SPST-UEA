from __future__ import annotations

import hashlib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "aether-refine-code"
SOURCE_SHA256 = "4ca0b5f6bba21eecbcf08e96baf3d30bb39de5487c27eee949b328b9a59a1b01"


def _frontmatter(markdown: str) -> dict[str, str]:
    opening, payload, _ = markdown.split("---", maxsplit=2)
    assert opening == ""
    fields: dict[str, str] = {}
    for line in payload.strip().splitlines():
        key, value = line.split(":", maxsplit=1)
        fields[key.strip()] = value.strip()
    return fields


def test_aether_skill_has_discoverable_metadata_and_no_placeholders() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = _frontmatter(skill)

    assert metadata["name"] == "aether-refine-code"
    assert "simplify" in metadata["description"]
    assert "automatic memory writes" in metadata["description"]
    assert "TODO" not in skill


def test_aether_skill_fails_closed_at_spst_scope_and_memory_boundaries() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "inside the already authorized change surface" in skill
    assert "four control-flow levels as a review signal" in skill
    assert "Do not create or append `AETHER_MEMORY.md` automatically" in skill
    assert "producer Receipt attribution" in skill
    assert "If semantic equivalence cannot be established" in skill


def test_aether_ui_metadata_invokes_the_exact_skill_name() -> None:
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "AETHER Code Refinement"' in metadata
    assert 'default_prompt: "Use $aether-refine-code ' in metadata


def test_original_aether_prompt_is_preserved_byte_for_byte() -> None:
    source = (SKILL_ROOT / "references" / "original-system-prompt.md").read_bytes()

    assert hashlib.sha256(source).hexdigest() == SOURCE_SHA256


def test_repository_guidance_connects_aether_to_spst_contracts() -> None:
    agents = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    master = (
        REPOSITORY_ROOT / "docs" / "codex-autonomous-engineering-master-prompt-spst.md"
    ).read_text(encoding="utf-8")

    for document in (agents, readme, master):
        assert "aether-refine-code" in document
        assert "AETHER_MEMORY.md" in document
        assert "RuleCrystal" in document
