"""Versioned AI prompt templates."""

from __future__ import annotations

from src.utils.ai_prompts import PROMPT_VERSION, render_prompt


def test_render_enhanced_approve_includes_version() -> None:
    text = render_prompt(
        "enhanced_approve",
        min_bps=45,
        max_vol=80,
        signal_json='{"strategy":"cex_dex"}',
    )
    assert PROMPT_VERSION in text or "2026" in text
    assert "45" in text
    assert "cex_dex" in text


def test_unknown_prompt_raises() -> None:
    try:
        render_prompt("not_a_real_prompt")
        assert False, "expected KeyError"
    except KeyError:
        pass
