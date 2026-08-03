# -*- coding: utf-8 -*-
"""QMT session 单实例锁：防双引擎抢同一 session（connect -1 根因防御）。

物理意图（[[qmt-connect-1-rootcause]] 教训）：uvicorn 端口 8000 已拦同端口双起
（C-5 V1），但 SERVER_PORT 被覆盖或直跑 server 时第二个引擎仍可能连同一
QMT_SESSION_ID → xtquant 返 -1（session 占用）→ 全天锁死。本锁以 session 为键
（非端口）做进程级排他，live 模式 bootstrap 连网关前 acquire，拿不到即拒连。
"""
import os

import pytest

from trading import single_instance


def test_acquire_returns_lock_then_second_acquire_conflicts(tmp_path):
    """首实例拿到锁；第二实例同 session acquire → None（排他）。"""
    lock = single_instance.acquire("sess-1", lock_dir=str(tmp_path))
    assert lock is not None
    assert lock.path.exists()
    second = single_instance.acquire("sess-1", lock_dir=str(tmp_path))
    assert second is None
    lock.release()


def test_release_allows_reacquire(tmp_path):
    """release 后同 session 可再次 acquire（优雅停机释放）。"""
    first = single_instance.acquire("sess-1", lock_dir=str(tmp_path))
    first.release()
    second = single_instance.acquire("sess-1", lock_dir=str(tmp_path))
    assert second is not None
    second.release()


def test_lock_file_records_pid(tmp_path):
    """锁文件内容含当前 pid（排障可查持有者）。"""
    lock = single_instance.acquire("sess-1", lock_dir=str(tmp_path))
    pid_file = tmp_path / "trading_engine_sess-1.pid"
    assert pid_file.exists()
    assert str(os.getpid()) in pid_file.read_text(encoding="utf-8")
    lock.release()


def test_different_sessions_do_not_conflict(tmp_path):
    """不同 session 各自独立成锁（多账户/多 session 不互斥）。"""
    a = single_instance.acquire("sess-1", lock_dir=str(tmp_path))
    b = single_instance.acquire("sess-2", lock_dir=str(tmp_path))
    assert a is not None and b is not None
    a.release()
    b.release()
