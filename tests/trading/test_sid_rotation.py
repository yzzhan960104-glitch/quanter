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
    """首选 -1 → 自动换 123460 并写 M2 真相源 + json 快照（.env preferred 不动）。"""
    import asyncio
    gw = QmtExecutionGateway(userdata_path=str(tmp_path), account_id="t",
                             session_id=123459)
    gw._loop = asyncio.get_running_loop()

    async def fake_bootstrap(sid):
        return (0, 0)

    monkeypatch.setattr(gw, "_run_bootstrap", fake_bootstrap)
    written = {}
    # T14：writer 签名加 account_id（M2 轮换点直写 DB 真相源），recorder 同步收
    monkeypatch.setattr(qmt_mod, "_write_runtime_session",
                        lambda p, a, account_id=None: written.update(
                            preferred=p, actual=a, account_id=account_id))

    sub = await gw._try_rotate_session()
    assert sub == 0
    assert gw._session_id == 123460
    # account_id 必须透传（否则轮换点 DB 写口无的放矢，actual_sid 真相源滞后到重启）
    assert written == {"preferred": 123459, "actual": 123460, "account_id": "t"}


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
    # T14：轮换成功会真写 json 快照 + DB 真相源——测试里挂个哑 writer 隔离副作用
    #（旧行为会顺手把 repo logs/engine_session.json 写脏；DB 写口更碰不得生产库）。
    monkeypatch.setattr(qmt_mod, "_write_runtime_session",
                        lambda p, a, account_id=None: None)
    await gw._try_rotate_session()
    assert used_sids == [123461]  # 123460 在用 → 跳过


# ============================================================================
# M2 actual_sid 单 SSoT（T14）：DB account.session_id 唯一真相源读写口。
# json 降级为运行态快照（仅供人眼），supervisor 只读 DB；旧「session_id 死键
# 兼容」随读取侧整体退役（双源判断即双源漂移，读 JSON 的路径全删）。
# ============================================================================
def test_write_runtime_session_updates_db_ssot(tmp_db, monkeypatch):
    """M2 写口：writer 把 actual 写进 DB 真相源（列级精准，不抹配置列）。"""
    from trading import state_store
    state_store.upsert_account("t", "qmt", session_id=123459,
                               mode="live", db_path=tmp_db)
    qmt_mod._write_runtime_session(123459, 123460, account_id="t")
    assert state_store.get_session_id("t", db_path=tmp_db) == 123460
    acc = state_store.get_account("t", db_path=tmp_db)
    assert acc["mode"] == "live"  # set_session_id 列级 UPDATE，配置列存活


def test_write_runtime_session_db_failure_does_not_raise(tmp_db, monkeypatch):
    """M2 写口降级语义：DB 写失败只告警不抛（连接已成功，观测缺值不阻断交易）。"""
    from trading import state_store

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(state_store, "set_session_id", _boom)
    # 不抛即过（json 快照照写；异常被吞进 warning 日志）
    qmt_mod._write_runtime_session(123459, 123460, account_id="t")


def test_write_runtime_session_without_account_skips_db(tmp_db):
    """M2：account_id 缺省（None）→ 跳过 DB 写口只写 json 快照（不猜账户）。"""
    from trading import state_store
    qmt_mod._write_runtime_session(123459, 123460)  # 无 account_id，不得炸/不得写 DB
    assert state_store.get_session_id("t", db_path=tmp_db) is None


def test_read_runtime_session_reads_db_ssot(tmp_db, monkeypatch):
    """M2 读口：supervisor actual_sid 改读 DB 真相源（经 state_store.get_session_id）。"""
    from trading import state_store
    state_store.upsert_account("ACC_SUP", "qmt", session_id=123460, db_path=tmp_db)
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_SUP")
    from ops import trading_supervisor as ts
    assert ts._read_runtime_session() == 123460


def test_read_runtime_session_none_when_no_db_row(tmp_db, monkeypatch):
    """M2 读口诚实语义：DB 无该账户行 → None（不回退读 json 死源，双源即漂移）。"""
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_GHOST")
    from ops import trading_supervisor as ts
    assert ts._read_runtime_session() is None
