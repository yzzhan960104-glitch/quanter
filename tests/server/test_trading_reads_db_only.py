# -*- coding: utf-8 -*-
"""三函数 DB-only 契约真红测试（SSoT Phase A · Task A2，A4 收口）。

物理意图（为什么需要这组测试）：
    旧实现里 aggregate_fills_by_symbol / export_trades / query_trades 三函数都
    保留 `LIVE_TRADE_READ_SOURCE=csv` 一键回退读口 + DB 异常自动回退 CSV 镜像。
    SSoT 红线是「fill 表是成交流水唯一真相源」（spec §2.4），CSV 在重放/补推场景
    下会重复（08-04 事故根因）。A2 已把读回退彻底删掉，让消费端只能读 DB。

    本文件用「真红纪律」证明回退被删（DB 空 → 三函数返空，磁盘有 CSV 不影响）：
      - 写真实 CSV 行到磁盘（utf-8-sig BOM + DictWriter，复刻原 record_live_trade
        写盘同口径，A4 删 record_live_trade 后此仅是「磁盘上有历史 CSV 残留」的
        场景模拟，服务已无 LIVE_TRADE_LOG 常量 / 无 CSV 读分支，不可能回退）

A4 收口（原 A2 真红测试的 monkeypatch LIVE_TRADE_LOG 失效）：
    - LIVE_TRADE_LOG 常量 A4 删，monkeypatch.setattr(svc, "LIVE_TRADE_LOG", ...)
      会 AttributeError。删 monkeypatch 行 + 删 setenv 行（env 也不再读，A2 已删）。
    - 测试物理意图保持：DB 空 + 磁盘有 CSV 残留 → 三函数返空（不读磁盘 CSV）。

关联：
    - A0/A1 已就绪（tmp_db fixture + state_store.query_fills + build_trade_id）
    - 既有 tests/server/test_trading_trades.py A4 已平移到 DB 契约（不再测 CSV 读口）
"""
import csv
import os


def _write_csv_row(log_path, *, symbol="600000.SH", kind="fill",
                   direction="BUY", shares=100, price=10.0):
    """写真实 CSV 行（utf-8-sig BOM + DictWriter，复刻原 record_live_trade 写盘口径）。

    Why utf-8-sig：原 record_live_trade 用 utf-8-sig 写盘（带 BOM，方便 Excel 直接打开），
    读口也用 utf-8-sig 解码；测试复刻同口径。A4 删 record_live_trade 后此仅是历史
    CSV 残留场景的模拟，证明服务不会回退读磁盘残留 CSV。
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    cols = ["timestamp", "symbol", "direction", "shares", "price",
            "strategy", "rationale", "kind"]
    is_new = (not os.path.exists(log_path)) or os.path.getsize(log_path) == 0
    with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if is_new:
            w.writeheader()
        w.writerow({
            "timestamp": "2026-08-05 10:00:00",
            "symbol": symbol,
            "direction": direction,
            "shares": shares,
            "price": price,
            "strategy": "neckline",
            "rationale": "",
            "kind": kind,
        })


def test_query_trades_no_csv_fallback_when_db_empty(tmp_db, tmp_path):
    """DB 空 + 磁盘有 CSV 残留行：新代码 DB-only 返空 (total=0)。

    A4 收口：原测试 monkeypatch LIVE_TRADE_LOG + setenv=csv 强制走 CSV 分支证明
    回退已删；A4 删 LIVE_TRADE_LOG 常量后两行 monkeypatch 失效。保留「磁盘有 CSV
    残留 + DB 空 → 返空」的物理意图（服务无 LIVE_TRADE_LOG 常量 / 无 CSV 读分支，
    磁盘残留 CSV 不影响输出）。
    """
    import trading.gateway_service as svc
    csv_log = tmp_path / "live_trades.csv"
    _write_csv_row(str(csv_log))
    # 不 monkeypatch（A4 删 LIVE_TRADE_LOG 常量后无此属性可 patch）；磁盘 CSV 残留
    # 服务读不到（无 CSV 读分支），DB 空 → 返空。
    res = svc.query_trades("2000-01-01", "2099-12-31")
    assert res["total"] == 0


def test_aggregate_fills_db_exception_returns_empty(tmp_db, monkeypatch, tmp_path):
    """patch query_fills 抛错 + 磁盘有 CSV 残留：新代码返 {}（不回退 CSV）。

    Why 必须验证异常分支：旧代码「DB 异常 → 自动回退 CSV」是观测层纪律的体现，
    SSoT 红线要把它也删掉（fill 表是真相源，DB 异常应当 logger.exception +
    返空，绝不静默回退 CSV 让消费端拿到幻象数据）。

    A4 收口：删 monkeypatch LIVE_TRADE_LOG + setenv（常量已删，无回退路径）。
    """
    from trading import state_store
    import trading.gateway_service as svc
    csv_log = tmp_path / "live_trades.csv"
    _write_csv_row(str(csv_log))
    # 让 query_fills 抛 RuntimeError —— 新代码 logger.exception + 返 {}（不回退 CSV）
    monkeypatch.setattr(state_store, "query_fills",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert svc.aggregate_fills_by_symbol("2000-01-01", "2099-12-31") == {}


def test_export_trades_carries_strategy_value(tmp_db):
    """export 读 fill.strategy 值（A1 加列，Fix 2 修 export 不再硬编码空）。

    物理意图：A1 给 fill 表加 strategy 列、A3 让 query_fills SELECT 返 strategy 字段，
    export_trades 作为消费端必须把 fill.strategy 透出（策略归属是复盘/过滤的真相字段）。
    旧代码 ``"strategy": ""`` 硬编码空 → strategy 字段在导出流水里永久丢失，违反 SSoT
    「fill 表是成交流水唯一真相源」契约（消费端拿不到真相源字段）。
    """
    from trading import state_store
    import trading.gateway_service as svc
    # 落 1 行带 strategy="neckline" 的 fill（A1 加列 + A3 SELECT 已就绪）
    state_store.insert_fill(
        "O1", "ACC_TEST", "20260805101000", "600000.SH", "BUY", 100, 10.0,
        strategy="neckline")
    out = svc.export_trades("2026-08-05", "2026-08-05")
    # strategy 值应出现（旧代码硬编码 "" → 此断言 FAIL）
    assert "neckline" in out, f"export 未透出 fill.strategy 值：\n{out}"


def test_query_trades_carries_strategy_value(tmp_db):
    """query 读 fill.strategy 值（A1 加列，Fix 2 修 query 不再硬编码空）。

    与 export 同语义：query_trades 是前端 TradesPage 的数据源，strategy 字段丢失
    会让前端流水展示「无策略归属」，复盘/过滤功能失效。
    """
    from trading import state_store
    import trading.gateway_service as svc
    state_store.insert_fill(
        "O1", "ACC_TEST", "20260805101000", "600000.SH", "BUY", 100, 10.0,
        strategy="neckline")
    res = svc.query_trades("2026-08-05", "2026-08-05")
    assert res["trades"], "query_trades 未返成交行"
    # strategy 值应为 "neckline"（旧代码硬编码 "" → 此断言 FAIL）
    assert res["trades"][0]["strategy"] == "neckline", (
        f"query_trades 未透出 fill.strategy 值：{res['trades'][0]}")


def test_export_trades_db_empty_header_only(tmp_db, tmp_path):
    """DB 空 + 有 CSV 残留：新代码返仅表头（不读 CSV）。

    A4 收口：原测试 monkeypatch LIVE_TRADE_LOG + setenv 证明不回退；A4 删常量后
    服务无 CSV 读分支，磁盘 CSV 残留不影响 export_trades（DB 空 → 仅表头一行）。
    """
    import trading.gateway_service as svc
    csv_log = tmp_path / "live_trades.csv"
    _write_csv_row(str(csv_log))
    out = svc.export_trades("2000-01-01", "2099-12-31")
    # 仅表头一行（旧代码回退读 CSV 会返表头+1数据行）
    assert out.count("\n") == 1
