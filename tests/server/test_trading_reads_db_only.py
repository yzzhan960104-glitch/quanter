# -*- coding: utf-8 -*-
"""三函数 DB-only 契约真红测试（SSoT Phase A · Task A2）。

物理意图（为什么需要这组测试）：
    旧实现里 aggregate_fills_by_symbol / export_trades / query_trades 三函数都
    保留 `LIVE_TRADE_READ_SOURCE=csv` 一键回退读口 + DB 异常自动回退 CSV 镜像。
    SSoT 红线是「fill 表是成交流水唯一真相源」（spec §2.4），CSV 在重放/补推场景
    下会重复（08-04 事故根因）。Phase A 必须把读回退彻底删掉，让消费端只能读 DB。

    本文件用「真红纪律」证明回退被删：
      - 写真实 CSV 行到磁盘（utf-8-sig BOM + DictWriter，与 record_live_trade 写盘同口径）
      - monkeypatch LIVE_TRADE_LOG 指向它
      - monkeypatch LIVE_TRADE_READ_SOURCE=csv（强制走旧 CSV 分支）
      - DB 空（tmp_db fixture）
    旧代码会回退读 CSV → 返非空；新代码 DB-only → 返空。
    断言「返空」在旧代码下 FAIL = 真红证明测试有效；新代码下 PASS = 回退真删了。

关联：
    - A0/A1 已就绪（tmp_db fixture + state_store.query_fills + build_trade_id）
    - 既有 tests/server/test_trading_trades.py 用 LIVE_TRADE_READ_SOURCE=csv 锁
      CSV 读口契约，A2 后会 FAIL（属 A4 重组范围，本 task 不改）
"""
import csv
import os


def _write_csv_row(log_path, *, symbol="600000.SH", kind="fill",
                   direction="BUY", shares=100, price=10.0):
    """写真实 CSV 行（utf-8-sig BOM + DictWriter，与 record_live_trade 写盘同口径）。

    Why utf-8-sig：record_live_trade 用 utf-8-sig 写盘（带 BOM，方便 Excel 直接打开），
    读口也用 utf-8-sig 解码；测试必须复刻同口径，否则DictReader 把 BOM 粘到首列名上
    （"\\ufefftimestamp"），行 dict 全 None，旧代码 CSV 回退也返空 → 真红失效。
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


def test_query_trades_no_csv_fallback_when_db_empty(tmp_db, monkeypatch, tmp_path):
    """DB 空 + 磁盘有 CSV 行 + LIVE_TRADE_READ_SOURCE=csv：旧代码回退返 CSV 行
    (total=1)；新代码 DB-only 返空 (total=0)。真红证明 CSV 回退已删。"""
    import presentation.server.services.trading_service as svc
    csv_log = tmp_path / "live_trades.csv"
    _write_csv_row(str(csv_log))
    monkeypatch.setattr(svc, "LIVE_TRADE_LOG", str(csv_log))
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")  # 强制走 CSV 分支（旧代码）
    res = svc.query_trades("2000-01-01", "2099-12-31")
    # 新代码：DB-only 返空（旧代码会返 1 → 测试在旧代码下 FAIL = 真红）
    assert res["total"] == 0


def test_aggregate_fills_db_exception_returns_empty(tmp_db, monkeypatch, tmp_path):
    """patch query_fills 抛错 + 磁盘有 CSV（旧代码回退返净持仓）；新代码返 {}。

    Why 必须验证异常分支：旧代码「DB 异常 → 自动回退 CSV」是观测层纪律的体现，
    SSoT 红线要把它也删掉（fill 表是真相源，DB 异常应当 logger.exception +
    返空，绝不静默回退 CSV 让消费端拿到幻象数据）。"""
    from trading import state_store
    import presentation.server.services.trading_service as svc
    csv_log = tmp_path / "live_trades.csv"
    _write_csv_row(str(csv_log))
    monkeypatch.setattr(svc, "LIVE_TRADE_LOG", str(csv_log))
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    # 让 query_fills 抛 RuntimeError —— 旧代码会 catch 后回退 CSV 读盘返净持仓
    monkeypatch.setattr(state_store, "query_fills",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert svc.aggregate_fills_by_symbol("2000-01-01", "2099-12-31") == {}  # 不回退 CSV


def test_export_trades_db_empty_header_only(tmp_db, monkeypatch, tmp_path):
    """DB 空 + 有 CSV：新代码返仅表头（不读 CSV）。

    旧代码会回退读 CSV 返 表头 + 1 数据行（共两行，"\n" 计数 == 2）。
    新代码 DB-only 返仅表头一行（"\n" 计数 == 1）。"""
    import presentation.server.services.trading_service as svc
    csv_log = tmp_path / "live_trades.csv"
    _write_csv_row(str(csv_log))
    monkeypatch.setattr(svc, "LIVE_TRADE_LOG", str(csv_log))
    monkeypatch.setenv("LIVE_TRADE_READ_SOURCE", "csv")
    out = svc.export_trades("2000-01-01", "2099-12-31")
    # 仅表头一行（旧代码会返表头+1数据行 → 真红）
    assert out.count("\n") == 1
