# -*- coding: utf-8 -*-
"""L2：sid 自动轮换（spec §4.4 三级自愈 · 裁定 L1-L4）。"""
from __future__ import annotations

import pytest

# W2-H1：patch 内部全局（_write_runtime_session 等）须指真身模块——契约根
# broker.qmt_connection（broker.qmt 只是组装+re-export 垫片，patch 垫片无效）。
from broker import qmt_connection as qmt_mod
from broker.qmt import (
    QmtExecutionGateway,
    _candidate_session_ids,
    _used_session_ids,
)


def test_used_session_ids_extracts_sids(tmp_path):
    """down_queue/lock 文件名 → 在用 sid 集合（含 __mutex 后缀）。"""
    (tmp_path / "down_queue_win_123456").write_text("x")
    (tmp_path / "lock_down_queue_win_123458__mutex").write_text("x")
    (tmp_path / "xtmodel_other").write_text("x")
    assert _used_session_ids(str(tmp_path)) == {123456, 123458}


def test_used_session_ids_empty_when_dir_missing(tmp_path):
    assert _used_session_ids(str(tmp_path / "no_such")) == set()


def test_candidate_skips_used_and_bounded():
    used = {123460, 123465}
    cands = _candidate_session_ids(123459, used, limit=3)
    assert 123460 not in cands and 123465 not in cands
    assert len(cands) <= 3
    assert all(c > 123459 for c in cands)


@pytest.mark.asyncio
async def test_try_rotate_session_rotates_on_success(monkeypatch, tmp_path):
    """首选 -1 → 自动换 123460 并写 runtime SSoT（.env preferred 不动）。"""
    import asyncio
    gw = QmtExecutionGateway(userdata_path=str(tmp_path), account_id="t",
                             session_id=123459)
    gw._loop = asyncio.get_running_loop()

    async def fake_bootstrap(sid):
        return (0, 0)

    monkeypatch.setattr(gw, "_run_bootstrap", fake_bootstrap)
    written = {}
    monkeypatch.setattr(qmt_mod, "_write_runtime_session",
                        lambda p, a: written.update(preferred=p, actual=a))

    sub = await gw._try_rotate_session()
    assert sub == 0
    assert gw._session_id == 123460
    assert written == {"preferred": 123459, "actual": 123460}


@pytest.mark.asyncio
async def test_try_rotate_session_returns_none_when_exhausted(monkeypatch, tmp_path):
    """所有候选都 -1 → 返 None（L3 人工兜底），session 保持 preferred。"""
    import asyncio
    gw = QmtExecutionGateway(userdata_path=str(tmp_path), account_id="t",
                             session_id=123459)
    gw._loop = asyncio.get_running_loop()

    async def fake_bootstrap(sid):
        return (-1, -1)

    monkeypatch.setattr(gw, "_run_bootstrap", fake_bootstrap)
    sub = await gw._try_rotate_session()
    assert sub is None
    assert gw._session_id == 123459


@pytest.mark.asyncio
async def test_try_rotate_session_skips_used_sid(monkeypatch, tmp_path):
    """占用登记表里的 sid 不会被选中（撞车预防）。"""
    import asyncio
    (tmp_path / "down_queue_win_123460").write_text("x")
    gw = QmtExecutionGateway(userdata_path=str(tmp_path), account_id="t",
                             session_id=123459)
    gw._loop = asyncio.get_running_loop()
    used_sids: list[int] = []

    async def fake_bootstrap(sid):
        used_sids.append(sid)
        return (0, 0)

    monkeypatch.setattr(gw, "_run_bootstrap", fake_bootstrap)
    await gw._try_rotate_session()
    assert used_sids == [123461]  # 123460 在用 → 跳过


def test_read_runtime_session_matches_writer_keys(tmp_path, monkeypatch):
    """code-review：读取侧键名必须与 _write_runtime_session 写入键一致（actual 非 session_id）。"""
    import json
    from ops import trading_supervisor as ts

    p = tmp_path / "logs" / "engine_session.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"preferred": 123459, "actual": 123460,
                             "rotated_at": "2026-08-06T12:00:00"}), encoding="utf-8")
    monkeypatch.setattr(ts, "ROOT", tmp_path)
    assert ts._read_runtime_session() == 123460


def test_read_runtime_session_fallback_legacy_session_id(tmp_path, monkeypatch):
    """兼容旧键 session_id（早期文件）也能读出。"""
    import json
    from ops import trading_supervisor as ts

    p = tmp_path / "logs" / "engine_session.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": 123457}), encoding="utf-8")
    monkeypatch.setattr(ts, "ROOT", tmp_path)
    assert ts._read_runtime_session() == 123457
