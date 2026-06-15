"""DirectionAwareBrain regime + soft forward policy."""

from src.strategies.direction_aware_brain import DirectionAwareBrain


def test_detect_regime_from_direction() -> None:
    dab = DirectionAwareBrain()
    assert dab.detect_regime({"direction": "dex_cheap"}) == "dex_cheap"
    assert dab.detect_regime({"direction": "cex_cheap"}) == "cex_cheap"


def test_soft_forward_allow_in_dex_cheap() -> None:
    dab = DirectionAwareBrain()
    signal = {"direction": "dex_cheap", "gross_bps": 15.0, "ai_confidence": 70.0}
    assert dab.evaluate(signal) is True
    assert signal.get("soft_forward_allow") is True
    assert signal.get("priority") == "medium"


def test_hard_block_weak_dex_cheap() -> None:
    dab = DirectionAwareBrain()
    signal = {"direction": "dex_cheap", "gross_bps": 5.0, "ai_confidence": 50.0}
    assert dab.evaluate(signal) is False
    assert signal.get("block_reason") == "wrong_direction_dex_cheap"


def test_gate_cex_dex_direction_soft_allow() -> None:
    from src.strategies.cex_dex_core import gate_cex_dex_direction

    opp = {
        "direction": "dex_cheap",
        "gross_bps": 14.0,
        "ai_confidence": 68.0,
        "jup_price": 100.0,
        "cex_price": 101.0,
    }
    assert gate_cex_dex_direction(opp) is None
    assert opp.get("soft_forward_allow") is True
