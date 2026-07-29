# -*- coding: utf-8 -*-
"""M5 session 清锁脚本单测：list + 可清性判定（不真删文件，纯逻辑）。"""
import os, time, importlib.util, sys
import pytest

def _load_module():
    """从 scripts/ 加载（非 package，importlib 加载）。"""
    spec = importlib.util.spec_from_file_location(
        "qmt_clear_session_lock", os.path.join("scripts", "qmt_clear_session_lock.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

@pytest.fixture
def mod(): return _load_module()

def test_list_session_locks_finds_queue_files(mod, tmp_path):
    """list 能识别 down_queue_win_*/lock_*/mutex 并归 sid。"""
    (tmp_path / "down_queue_win_123456").write_bytes(b"x")
    (tmp_path / "down_queue_win_123456__mutex").write_bytes(b"")
    (tmp_path / "lock_down_queue_win_123458").write_bytes(b"")
    (tmp_path / "miniqmtShmStockListCacheSZO").write_bytes(b"x")  # 非锁文件，应忽略
    locks = mod.list_session_locks(str(tmp_path))
    sids = {l["sid"] for l in locks}
    assert sids == {123456, 123458}  # 123458 来自 lock_down_queue_win_123458 归属

def test_is_clearable_rejects_current_sid(mod):
    """当前 sid 的锁一律不清（防误删活跃队列）。"""
    now = time.time()
    lock = {"sid": 123458, "mtime": now - 9999, "path": "x"}
    assert mod.is_clearable(lock, current_sid=123458, now=now) is False

def test_is_clearable_rejects_recent_file(mod):
    """近 1h 活跃的锁不清（可能在用）。"""
    now = time.time()
    lock = {"sid": 999, "mtime": now - 100, "path": "x"}  # 100s 前，<1h
    assert mod.is_clearable(lock, current_sid=123458, now=now) is False

def test_is_clearable_accepts_old_noncurrent(mod):
    """非当前 sid 且 mtime>1h 的残留可清。"""
    now = time.time()
    lock = {"sid": 123456, "mtime": now - 7200, "path": "x"}  # 2h 前，非当前
    assert mod.is_clearable(lock, current_sid=123458, now=now) is True
