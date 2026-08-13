# -*- coding: utf-8 -*-
"""L0 快照冻结测试：纯函数优先（快），freeze 集成标 slow。"""
import pandas as pd
import pytest


def test_snapshot_hash_deterministic():
    """同输入 → 同 hash（可复现性基石，spec ADR3）。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    assert h1 == h2


def test_snapshot_hash_differs_on_count():
    """universe 数量变 → hash 变（探查实证的 1334→1332 必须能检出）。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1332, "2025-01-01~2026-07-23", "2025-01-01")
    assert h1 != h2


def test_snapshot_hash_differs_on_date_range():
    """日期范围变（数据湖增量落湖）→ hash 变。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1334, "2025-01-01~2026-07-30", "2025-01-01")
    assert h1 != h2


def _mini_universe():
    """两 symbol 迷你 universe（close 序列稳定可复现，供内容指纹测试）。"""
    idx = pd.date_range("2025-01-01", periods=3)
    return {
        "300001.SZ": pd.DataFrame({"close": [10.0, 10.5, 11.0]}, index=idx),
        "300002.SZ": pd.DataFrame({"close": [20.0, 19.5, 21.0]}, index=idx),
    }


def test_data_content_hash_stable_and_sensitive():
    """P1-3（2026-08-03）：价格内容指纹——同内容稳定；历史价变动（qfq 重算）必变。"""
    from discovery.snapshot import data_content_hash
    u = _mini_universe()
    h1 = data_content_hash(u, "2025-01-01")
    assert h1 == data_content_hash(_mini_universe(), "2025-01-01")   # 同内容同 hash
    u2 = _mini_universe()
    u2["300001.SZ"].iloc[1, 0] = 99.0   # 模拟除权重算历史价（qfq 基准漂移）
    assert data_content_hash(u2, "2025-01-01") != h1


def test_freeze_meta_includes_data_hash(monkeypatch):
    """freeze 产出 SnapshotMeta.data_hash（与内容指纹一致，落库审计用）。"""
    from discovery import snapshot
    monkeypatch.setattr(snapshot, "load_universe",
                        lambda start="2025-01-01": _mini_universe())
    u, meta = snapshot.freeze(lake_start="2025-01-01")
    assert meta.data_hash == snapshot.data_content_hash(u, "2025-01-01")
    assert len(meta.data_hash) == 16


def test_write_snapshot_persists_data_hash(tmp_path):
    """store.write_snapshot 落 data_hash 列（老库 ALTER 迁移后亦可用）。"""
    from discovery.snapshot import SnapshotMeta
    from discovery.store import connect, init_db, write_snapshot
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "u", 10, "2025~2026", "2025-01-01", data_hash="abc123")
    with connect(db) as conn:
        write_snapshot(conn, meta)
        row = conn.execute(
            "SELECT data_hash FROM snapshot WHERE snapshot_hash='snap1'").fetchone()
    assert row["data_hash"] == "abc123"


def test_load_universe_reads_with_date_filter(monkeypatch):
    """回归（2026-08-03 资源优化）：read_parquet 必须带 filters（date>=start）。

    旧实现先全量读 1019 万行再筛 → 每 worker ~1.3GB；12 worker 并发加载是
    内存峰值根因。filter 推到读层后 2025 起只读 ~200 万行（~0.3GB）。
    """
    from discovery import snapshot
    calls = {}

    def fake_read(path, **kwargs):
        calls.update(kwargs)
        idx = pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2025-01-02"), "300001.SZ"),
             (pd.Timestamp("2025-01-03"), "300001.SZ"),
             (pd.Timestamp("2025-01-02"), "600000.SH")],
            names=["date", "symbol"],
        )
        return pd.DataFrame({
            "open": [10.0, 11.0, 20.0],
            "high": [11.0, 12.0, 21.0],
            "low": [9.0, 10.0, 19.0],
            "close": [10.5, 11.5, 20.5],
            "volume": [1000.0, 1100.0, 2000.0],
            "amount": [1e5, 1.1e5, 2e5],
        }, index=idx)

    monkeypatch.setattr(snapshot.pd, "read_parquet", fake_read)
    universe = snapshot.load_universe(start="2025-01-01")
    assert "filters" in calls
    assert calls["filters"] == [("date", ">=", pd.Timestamp("2025-01-01"))]
    # 创板科创 + 流动性过滤后只剩 300001.SZ
    assert set(universe.keys()) == {"300001.SZ"}


@pytest.mark.slow
def test_freeze_loads_real_universe():
    """集成：freeze 真实加载（~5s），返回非空 universe + 元数据。需 data_lake。"""
    from discovery.snapshot import freeze
    universe, meta = freeze()
    assert len(universe) > 100                       # 创板科创应有数百只
    assert meta.universe_count == len(universe)
    assert len(meta.snapshot_hash) == 16
    assert "2025" in meta.date_range


def test_filter_universe_from_lake_synthetic():
    """P5 分折 universe 纯函数：合成湖（含退市标的 + 低流动性标的）→ 过滤口径正确。

    幸存者偏差防线（spec §6.1）：流动性 tail(30) 取自传入窗（折末口径）——退市标的
    只要在窗内挂牌且满足流动性就保留（折后自然消失），低流动性/非创板科创剔除。
    """
    import pandas as pd
    from discovery.snapshot import filter_universe_from_lake

    idx = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-01", periods=40, freq="B"),
         ["300001.SZ", "300002.SZ", "600000.SH"]],
        names=["date", "symbol"])
    amount = pd.Series(100_000.0, index=idx)          # 创板 2 只 ≥1e5；主板 1 只同额
    amount.loc[(slice(None), "300002.SZ")] = 10.0     # 300002 低流动性（<1e5）
    lake = pd.DataFrame({"amount": amount, "close": 1.0})
    lake["high"] = 1.0; lake["low"] = 1.0; lake["volume"] = 1.0

    univ = filter_universe_from_lake(lake)
    assert set(univ.keys()) == {"300001.SZ"}          # 300002 流动性不足；主板被 board 过滤
    assert len(univ["300001.SZ"]) == 40


def test_count_stale_symbols_offline_proxy():
    """P6-D2：尾部陈旧标的计数（离线连续性代理）——早于全局最新日超阈值者计陈旧。"""
    import pandas as pd
    from discovery.snapshot import _count_stale_symbols

    idx_new = pd.date_range("2026-07-01", "2026-08-07", freq="B")
    idx_stale = pd.date_range("2026-07-01", "2026-07-10", freq="B")
    cols = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.0, "volume": 1.0, "amount": 1e6}
    univ = {
        "300001.SZ": pd.DataFrame(cols, index=idx_new),
        "300002.SZ": pd.DataFrame(cols, index=idx_stale),   # 尾部陈旧（早于最新日 28 天）
        "300003.SZ": pd.DataFrame(cols, index=idx_new),
    }
    assert _count_stale_symbols(univ, stale_days=14) == 1


def test_snapshot_hash_includes_stale_count():
    """P6-D2：n_stale 入指纹——连续性状态变化 → hash 变（补采后收敛重置的触发源）。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1190, "2025-01-02~2026-08-07", "2025-01-01", n_stale=0)
    h2 = snapshot_hash(1190, "2025-01-02~2026-08-07", "2025-01-01", n_stale=3)
    assert h1 != h2
