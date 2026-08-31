from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_root_agents_is_short_and_routes_to_governance() -> None:
    content = read("AGENTS.md")
    assert len(content) < 3_000
    assert "docs/ARCHITECTURE_GUARDRAILS.md" in content
    assert "docs/development/CONTEXT_POLICY.md" in content
    assert "DESIGN_CONFLICT" in content
    assert "DESIGN_CHANGE_REQUIRED" in content


def test_guardrail_ids_are_unique_and_contiguous() -> None:
    content = read("docs/ARCHITECTURE_GUARDRAILS.md")
    ids = re.findall(r"\*\*G-(\d{3})\*\*", content)
    assert ids == [f"{value:03d}" for value in range(1, 49)]
    assert "known_at >= fetch_time" in content
    assert "known_at <= replay_as_of/context_as_of" in content


def test_phase_capsule_template_and_phase6_are_complete() -> None:
    required = {"SCOPE.md", "CONTRACTS.md", "ACCEPTANCE.md", "STATUS.md"}
    for directory in ("docs/phases/_template", "docs/phases/phase6"):
        assert required == {
            item.name for item in (ROOT / directory).iterdir() if item.suffix == ".md"
        }
    phase6_scope = read("docs/phases/phase6/SCOPE.md")
    assert "MUST NOT override the V3 Architecture Baseline" in phase6_scope
    assert "Phase 7" in phase6_scope


def test_current_state_routes_to_one_next_task() -> None:
    state = read("docs/工作状态.md")
    phase = read("docs/phases/phase6/STATUS.md")
    assert "phases/phase6/STATUS.md" in state
    assert "P6-01" in state
    assert "Current Task**：NONE" in phase
    assert "不自动进入" in phase


def test_v3_local_agents_preserves_legacy_boundary() -> None:
    content = read("app/v3/AGENTS.md")
    assert "不要为 V3 任务侵入" in content
    assert "Domain 不依赖 FastAPI、SQLAlchemy" in content
    assert "Replay 只能读取 `known_at <= replay_as_of`" in content
