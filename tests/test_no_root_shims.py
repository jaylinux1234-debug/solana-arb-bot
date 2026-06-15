"""Ensure legacy root shim modules were removed after src/ migration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REMOVED_SHIMS = [
    "cex_executor.py",
    "jupiter_executor.py",
    "openai_helper.py",
    "cex_dex_strategy.py",
    "wallet_safety.py",
    "health_api.py",
    "config.py",
    "kamino_helper.py",
    "jito_helper.py",
    "phoenix.py",
]


def test_root_shims_removed():
    for name in REMOVED_SHIMS:
        assert not (ROOT / name).is_file(), f"remove root shim: {name}"


def test_src_entrypoints_exist():
    assert (ROOT / "src" / "main.py").is_file()
    assert (ROOT / "main.py").is_file()
