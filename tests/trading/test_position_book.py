# -*- coding: utf-8 -*-
"""position_book 单测：本地持仓账本读写/幂等/方向/清理。

物理意图：验证对账「本地侧」单一真理源的 ACID 行为——
- BUY 累加 / SELL 累减 / 归零清理；
- UNIQUE(order_id) 幂等防重推（R1 红线：重推不重复加减持仓）；
- 方向未知抛 ValueError（不猜方向误记）；
- db_path 默认 None + 运行时解析 _DEFAULT_DB（e2e 可 monkeypatch）。
"""
from __future__ import annotations

import pytest

from trading import position_book


@pytest.fixture
def db(tmp_path, monkeypatch):
    """每个测试用独立 tmp db（隔离），并 patch _DEFAULT_DB 让 engine 间接调用也命中 tmp。"""
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(position_book, "_DEFAULT_DB", db_path)
    position_book.init_db()
    return db_path


def test_init_db_idempotent(db):
    """重复 init_db 不报错（CREATE TABLE IF NOT EXISTS 幂等）。"""
    position_book.init_db()  # 再次调
    position_book.init_db()  # 第三次
    # 不抛即通过


def test_apply_fill_buy_accumulates(db):
    """BUY 两次（不同 order_id）→ qty 累加。"""
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0) is True
    assert position_book.apply_fill("o2", "300001.SZ", "BUY", 200, 10.5) is True
    assert position_book.get_local_positions() == {"300001.SZ": 300.0}


def test_apply_fill_sell_decrements_and_clears_zero(db):
    """BUY 后 SELL → qty 减；归零则从 position 表删除（对账并集不被 0 干扰）。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0)
    position_book.apply_fill("o2", "300001.SZ", "SELL", 100, 11.0)  # 归零
    assert position_book.get_local_positions() == {}  # qty=0 已清理


def test_apply_fill_idempotent(db):
    """同 order_id 重推 → 返 False，qty 不变（R1 幂等红线）。"""
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0) is True
    assert position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0) is False  # 重推
    assert position_book.get_local_positions() == {"300001.SZ": 100.0}  # 没翻倍


def test_apply_fill_unknown_direction_raises(db):
    """direction 非 BUY/SELL → 抛 ValueError（不猜方向误记）。"""
    with pytest.raises(ValueError):
        position_book.apply_fill("o1", "300001.SZ", "TRADE", 100, 10.0)


def test_get_local_positions_excludes_zero(db):
    """qty=0 的标的（已清理）不返回；多标的混合正确。"""
    position_book.apply_fill("o1", "300001.SZ", "BUY", 100, 10.0)
    position_book.apply_fill("o2", "688001.SH", "BUY", 200, 20.0)
    position_book.apply_fill("o3", "688001.SH", "SELL", 200, 21.0)  # 688001 归零清理
    pos = position_book.get_local_positions()
    assert pos == {"300001.SZ": 100.0}
