# -*- coding: utf-8 -*-
"""fill 表契约测试（替代 test_live_trades_csv.py · SSoT Phase A · A4）。

Why 替代（设计意图，反黑盒）：
    旧 test_live_trades_csv.py 锁的是 CSV 写盘契约（record_live_trade → DictWriter
    utf-8-sig + LIVE_TRADE_COLUMNS 字段顺序），A4 删 record_live_trade 后写盘链路
    整体退役，CSV 不再是真相源。fill 表（state_store.fill，UNIQUE(order_id, traded_time)）
    接管成交流水真相源职责（spec §2.4），本文件锁 fill 表的三个契约红线：

      1. 幂等去重：同 (order_id, traded_time) 重推返 False、不重复记（08-04 事故根因）；
      2. 部分成交多行：同 order_id 不同 traded_time 各自一行，累加到 position；
      3. strategy 字段可读：query_fills SELECT 含 strategy（A3 补，闭合 A1 review 盲区），
         digest 消费端按 strategy 过滤依赖此字段。

    物理隔离：用 tmp_db fixture（A0 加，预置 ACC_TEST account），不污染真实 logs/。
"""
from trading import state_store


def test_insert_fill_dedup_on_unique(tmp_db):
    """幂等去重契约：UNIQUE(order_id, traded_time) —— 同笔成交重推第二次返 False。

    物理意图（08-04 事故根因）：CSV 在重放/补推下会重复 append（无 UNIQUE 约束），
    post_close 聚合出幻象持仓；fill 表的 UNIQUE 约束是事故后确立的真相源红线，
    本测试锁定此约束不退化（重推返 False + 查询只 1 行）。
    """
    ok1 = state_store.insert_fill("O1", "ACC_TEST", "20260805101000", "600000.SH", "BUY", 100, 10.0)
    ok2 = state_store.insert_fill("O1", "ACC_TEST", "20260805101000", "600000.SH", "BUY", 100, 10.0)
    assert ok1 is True, "首次写入应返 True"
    assert ok2 is False, "重复 (order_id, traded_time) 应被 UNIQUE 约束拒绝返 False"
    # 重推不重复记：查 [start, end] 只返 1 行（不是 2 行）
    rows = state_store.query_fills("2026-08-05", "2026-08-05")
    assert len(rows) == 1, f"幂等去重后应只 1 行，实际 {len(rows)}"


def test_query_fills_partial_fills_separate_rows(tmp_db):
    """部分成交多行契约：同 order_id 不同 traded_time 各自一行（不合并、不丢弃）。

    物理意图：QMT 部分成交（Partial Fill）会推送多次 kind=="trade" 回调，每次
    traded_time 不同（精确到秒），fill 表必须各自留痕——position 累加按多行求和，
    query_fills 明细展示也按原序返回（与 CSV 时间序一致，简报消费契约）。
    """
    state_store.insert_fill("O1", "ACC_TEST", "20260805101000", "600000.SH", "BUY", 100, 10.0)
    state_store.insert_fill("O1", "ACC_TEST", "20260805101030", "600000.SH", "BUY", 50, 10.5)
    rows = state_store.query_fills("2026-08-05", "2026-08-05")
    assert len(rows) == 2, f"两次部分成交应各 1 行，实际 {len(rows)}"
    # 升序：先 10:00 再 10:30
    assert rows[0]["traded_time"] == "20260805101000"
    assert rows[1]["traded_time"] == "20260805101030"


def test_query_fills_returns_strategy_field(tmp_db):
    """strategy 字段可读契约：query_fills 返回 dict 含 strategy 键（A3 补的 SELECT）。

    物理意图：A1 给 fill 表加了 strategy 列 + insert_fill 入参，但 SELECT 漏改
    （A1 review 盲区）；A3 补 SELECT strategy 后，digest 消费端按 strategy 过滤
    才能拿到值。本测试锁定 query_fills 必返 strategy 键，防 SELECT 再被漏改退化。
    """
    state_store.insert_fill(
        "O1", "ACC_TEST", "20260805101000", "600000.SH", "BUY", 100, 10.0,
        strategy="neckline",
    )
    rows = state_store.query_fills("2026-08-05", "2026-08-05")
    assert len(rows) == 1
    # strategy 字段必须在返回 dict 里（A3 SELECT 补全，A1 漏改的点）
    assert rows[0].get("strategy") == "neckline", \
        f"query_fills 必返 strategy 键，实际行：{rows[0]}"
