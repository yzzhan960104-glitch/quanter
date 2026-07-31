# tests/trading/test_state_store_data_ready.py
import tempfile, os
from pathlib import Path
from trading import state_store


def _fresh_db(monkeypatch):
    d = tempfile.mkdtemp()
    db = str(Path(d) / "t.db")
    monkeypatch.setattr(state_store, "_DEFAULT_DB", db)
    state_store.init_store()
    return db


def test_upsert_then_get(monkeypatch):
    db = _fresh_db(monkeypatch)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=True, melted=False,
                                  latest_date="2026-07-30", expected_date="2026-07-30",
                                  message="PASS", db_path=db)
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got is not None
    assert got["ok"] == 1
    assert got["dataset"] == "daily"


def test_get_missing_returns_none(monkeypatch):
    db = _fresh_db(monkeypatch)
    assert state_store.get_data_ready("2026-07-30", "daily", db_path=db) is None


def test_upsert_idempotent_overwrite(monkeypatch):
    db = _fresh_db(monkeypatch)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=False, melted=False,
                                  latest_date=None, expected_date="2026-07-30",
                                  message="缺", db_path=db)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=True, melted=False,
                                  latest_date="2026-07-30", expected_date="2026-07-30",
                                  message="PASS", db_path=db)
    got = state_store.get_data_ready("2026-07-30", "daily", db_path=db)
    assert got["ok"] == 1  # 第二次覆盖第一次


def test_multi_dataset_independent(monkeypatch):
    db = _fresh_db(monkeypatch)
    state_store.upsert_data_ready("2026-07-30", "daily", ok=True, melted=False,
                                  latest_date="2026-07-30", expected_date="2026-07-30",
                                  message="ok", db_path=db)
    state_store.upsert_data_ready("2026-07-30", "moneyflow", ok=False, melted=True,
                                  latest_date=None, expected_date="2026-07-30",
                                  message="缺", db_path=db)
    assert state_store.get_data_ready("2026-07-30", "daily", db_path=db)["ok"] == 1
    assert state_store.get_data_ready("2026-07-30", "moneyflow", db_path=db)["ok"] == 0
