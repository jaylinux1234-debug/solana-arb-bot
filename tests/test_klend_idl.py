"""Bundled KLend IDL presence and anchorpy load smoke tests."""

from __future__ import annotations

import pytest

from src.dex.klend_program import (
    LIQUIDATE_IX_NAME,
    klend_idl_path,
    klend_instruction_names,
    load_klend_idl_json,
    reset_klend_program_cache,
)


def test_klend_idl_file_present():
    path = klend_idl_path()
    assert path.is_file(), f"Missing {path} — run: npm run fetch:klend-idl"


def test_klend_idl_has_liquidate_instruction():
    reset_klend_program_cache()
    idl = load_klend_idl_json()
    assert idl.get("name") == "kamino_lending"
    names = klend_instruction_names()
    assert LIQUIDATE_IX_NAME in names


def test_anchorpy_parses_klend_idl():
    pytest.importorskip("anchorpy")
    from anchorpy import Idl

    reset_klend_program_cache()
    idl = Idl.from_json(klend_idl_path().read_text(encoding="utf-8"))
    assert idl.name == "kamino_lending"
    ix_names = {ix.name for ix in idl.instructions}
    assert LIQUIDATE_IX_NAME in ix_names
