# -*- coding: utf-8 -*-
"""掘金双腿日度对照（QuanterGmAbCompare · 2026-08-28 双轨方案 §4.4）。

物理意图：主腿(incumbent)与实验腿(challenger)同日历并行跑，每日 15:50（ingest
15:40 落数之后）从 experiments.db（terminal_audit 已按 account_id 分腿）拉两腿
当日事件做三面对照，回答一个工程问题：「candidate 的行为与 incumbent 的差异，
是否恰好等于我们宣称要改的那部分」。

对照面（MVP——校准轮够用，绩效/预算分类后置到候选轮）：
  ① 工程闸（硬）：各腿 INIT 双锚（account/build_stamp）、事件计数漏斗
     （SCHEDULE_TICK/SIGNAL/ORDER_PLACED/FILL/WARN）、WARN 分类（拒单/查询失败/…）；
  ② 信号对照（校准轮预期 diff=0）：SIGNAL 行 (symbol → entry_price) 对齐 →
     新增/消失/价漂移——零变量校准轮里任何 diff 都是红旗（两腿同参，差异只能
     来自未知的非确定性/环境差）；候选轮的 diff 人工归因到预登记变更面；
  ③ 订单对照（参考）：ORDER_PLACED 的 (symbol → price) diff + 数量差
     （qty 差=资金口径差，标注为预期，10 万 vs 12 亿权益的固有形态）。

数据源只读：experiments.db（ingest 已采集）+ 无（MVP 不打 7002——EOD 报告已覆盖
持仓/资金面，对照脚本专注事件流）。报告落 logs/emquant_ab_YYYY-MM-DD.txt + 钉钉
（全绿 INFO / 有 diff WARN）。exp 腿未部署（active_legs 只含 main）→ 单腿静默退出。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from ops import gm_ops_common as gc
from ops.gm_ops_common import notify

DB_PATH = ROOT / "experiment" / "experiments.db"

# 事件计数漏斗的键序（报告稳定序）
_COUNT_EVENTS = ("SCHEDULE_TICK", "SIGNAL", "ORDER_PLACED", "FILL",
                 "ORDER_BLOCKED", "CANCEL", "RECONCILE", "EOD")


def _leg_account(leg: gc.LegDef) -> str | None:
    """腿的期望账户：runtime.json 的 account_id（意图层真值）。缺文件 → None
    （该腿未部署语义，调用方跳过）。"""
    try:
        return str(gc.runtime_config(gc.leg_strategy_dir(leg)).get("account_id") or "")
    except (OSError, ValueError):
        return None


def fetch_rows(day: str, account_id: str, db_path: Path = DB_PATH) -> list[tuple]:
    """当日该腿的 (ts, event, detail) 行（按 ts 稳定序）。"""
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as con:
        return con.execute(
            "SELECT ts, event, detail FROM terminal_audit"
            " WHERE account_id=? AND ts LIKE ? ORDER BY ts",
            (account_id, day + "%")).fetchall()


def parse_json(detail: str | None) -> dict:
    try:
        d = json.loads(detail) if detail else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def eng_gate(rows: list[tuple]) -> dict:
    """工程闸统计：INIT 双锚 + 事件漏斗计数 + WARN 分类。"""
    st: dict = {"init": None, "counts": Counter(), "warn_types": Counter()}
    for _ts, ev, detail in rows:
        st["counts"][ev] += 1
        d = parse_json(detail)
        if ev == "INIT":
            st["init"] = {"account": d.get("account"), "build_stamp": d.get("build_stamp")}
        elif ev == "WARN":
            st["warn_types"][str(d.get("type") or "untyped")] += 1
    return st


def signals_map(rows: list[tuple]) -> dict[str, float]:
    """SIGNAL 行 → {symbol: entry_price}（同 symbol 多信号取最后一条——audit 追加序）。"""
    out: dict[str, float] = {}
    for _ts, ev, detail in rows:
        if ev != "SIGNAL":
            continue
        d = parse_json(detail)
        sym, ep = d.get("symbol"), d.get("entry_price")
        if sym is None or ep is None:
            continue
        try:
            out[str(sym)] = float(ep)
        except (TypeError, ValueError):
            continue
    return out


def orders_map(rows: list[tuple]) -> dict[str, dict]:
    """ORDER_PLACED 行 → {symbol: {price, qty}}（同上取最后一条）。"""
    out: dict[str, dict] = {}
    for _ts, ev, detail in rows:
        if ev != "ORDER_PLACED":
            continue
        d = parse_json(detail)
        sym = d.get("symbol")
        if sym is None:
            continue
        try:
            out[str(sym)] = {"price": float(d.get("price") or 0),
                             "qty": int(d.get("qty") or 0)}
        except (TypeError, ValueError):
            continue
    return out


def diff_float_maps(a: dict[str, float], b: dict[str, float],
                    rel_tol: float = 1e-9) -> dict:
    """两 {symbol: value} 的结构化 diff：新增/消失/值漂移。"""
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    drifted = []
    for s in sorted(set(a) & set(b)):
        va, vb = a[s], b[s]
        if abs(va - vb) > rel_tol * max(1.0, abs(va)):
            drifted.append({"symbol": s, "incumbent": va, "challenger": vb})
    return {"added": added, "removed": removed, "drifted": drifted}


def diff_orders(a: dict[str, dict], b: dict[str, dict]) -> dict:
    """订单对照：结构 diff 复用信号语义；qty 差单列（资金口径差=预期形态）。"""
    d = diff_float_maps({s: v["price"] for s, v in a.items()},
                        {s: v["price"] for s, v in b.items()})
    qty_diff = []
    for s in sorted(set(a) & set(b)):
        if a[s]["qty"] != b[s]["qty"]:
            qty_diff.append({"symbol": s, "incumbent": a[s]["qty"],
                             "challenger": b[s]["qty"]})
    d["qty_diff"] = qty_diff
    return d


def build_report(day: str, main_rows: list[tuple], exp_rows: list[tuple],
                 main_acc: str, exp_acc: str) -> tuple[str, list[str]]:
    """(报告全文, 红旗列表)。红旗=校准轮口径下的任何结构性 diff + 工程闸异常。"""
    g_a, g_b = eng_gate(main_rows), eng_gate(exp_rows)
    sig_a, sig_b = signals_map(main_rows), signals_map(exp_rows)
    ord_a, ord_b = orders_map(main_rows), orders_map(exp_rows)
    sig_d = diff_float_maps(sig_a, sig_b)
    ord_d = diff_orders(ord_a, ord_b)
    flags: list[str] = []

    def fmt_gate(label: str, g: dict, acc_expect: str, rows: list[tuple]) -> list[str]:
        lines = [f"—— {label} ——"]
        init = g["init"]
        if init:
            lines.append(f"INIT: account={init.get('account')} stamp={init.get('build_stamp')}")
            if str(init.get("account")) != acc_expect:
                flags.append(f"[{label}] INIT account={init.get('account')} ≠ 期望 {acc_expect}")
        elif rows:
            flags.append(f"[{label}] 当日有事件但无 INIT 行（跨日常驻形态，参考）")
        cnt = " ".join(f"{ev}×{g['counts'][ev]}" for ev in _COUNT_EVENTS if g["counts"][ev])
        lines.append(f"事件漏斗: {cnt or '（当日零事件）'}")
        if g["warn_types"]:
            lines.append("WARN: " + " ".join(f"{k}×{v}" for k, v in sorted(g["warn_types"].items())))
        return lines

    lines = [f"掘金双腿对照 · {day}（incumbent=主腿 challenger=实验腿）"]
    lines += fmt_gate("主腿", g_a, main_acc, main_rows)
    lines += fmt_gate("实验腿", g_b, exp_acc, exp_rows)

    lines.append("—— 信号对照（校准轮预期 diff=0）——")
    if sig_d["added"] or sig_d["removed"] or sig_d["drifted"]:
        flags.append(f"信号 diff：新增{sig_d['added']} 消失{sig_d['removed']} "
                     f"漂移{[(x['symbol']) for x in sig_d['drifted']]}")
        lines.append(f"新增: {sig_d['added'] or '—'}")
        lines.append(f"消失: {sig_d['removed'] or '—'}")
        lines.append(f"价漂移: {sig_d['drifted'] or '—'}")
    else:
        lines.append(f"完全一致（{len(sig_a)} 信号）" if sig_a else "两腿均零信号（对照面=空,弱一致）")

    lines.append("—— 订单对照（qty 差=资金口径差,预期形态）——")
    if ord_d["added"] or ord_d["removed"] or ord_d["drifted"]:
        flags.append(f"订单结构 diff：新增{ord_d['added']} 消失{ord_d['removed']} "
                     f"价漂移{[x['symbol'] for x in ord_d['drifted']]}")
        lines.append(f"新增: {ord_d['added'] or '—'}｜消失: {ord_d['removed'] or '—'}｜"
                     f"价漂移: {ord_d['drifted'] or '—'}")
    else:
        lines.append(f"结构一致（{len(ord_a)} 单）" if ord_a else "两腿均零挂单")
    if ord_d["qty_diff"]:
        lines.append(f"qty 差（资金口径,预期）: "
                     + " ".join(f"{x['symbol']} {x['incumbent']}→{x['challenger']}"
                                for x in ord_d["qty_diff"]))
    return "\n".join(lines), flags


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="掘金双腿日度对照（ingest 之后跑）")
    p.add_argument("--date", help="YYYY-MM-DD（缺省今天；回看用）")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--register", action="store_true", help="注册 15:50 每日 schtasks")
    p.add_argument("--unregister", action="store_true")
    args = p.parse_args(argv)
    if args.register or args.unregister:
        if args.register:
            bat = Path(__file__).parent / "run_ops_task.bat"
            rc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "15:50",
                                 "/TN", "QuanterGmAbCompare",
                                 "/TR", f'"{bat}" emquant_ab_compare.py'],
                                capture_output=True).returncode
            print("registered" if rc == 0 else f"failed rc={rc}")
            return rc
        subprocess.run(["schtasks", "/Delete", "/TN", "QuanterGmAbCompare", "/F"],
                       capture_output=True)
        print("unregistered")
        return 0

    now = datetime.now()
    if not args.date and now.weekday() >= 5:
        return 0                                    # 周末零事件,对照无意义,静默退出
    day = args.date or f"{now:%Y-%m-%d}"
    legs = gc.active_legs()
    if len(legs) < 2:
        print(f"[ab-compare] 单腿模式（{len(legs)} 腿在役），跳过对照")
        return 0
    main_leg = next(l for l in gc.LEGS if l.key == "main")
    exp_leg = next(l for l in gc.LEGS if l.key == "exp")
    main_acc, exp_acc = _leg_account(main_leg), _leg_account(exp_leg)
    if not main_acc or not exp_acc:
        print("[ab-compare] 腿账户解析失败（runtime.json 缺失?），跳过")
        return 0
    main_rows = fetch_rows(day, main_acc)
    exp_rows = fetch_rows(day, exp_acc)
    report, flags = build_report(day, main_rows, exp_rows, main_acc, exp_acc)
    print(report)
    log = ROOT / "logs" / f"emquant_ab_{day}.txt"
    log.parent.mkdir(exist_ok=True)
    log.write_text(report, encoding="utf-8")
    if not args.no_push:
        if flags:
            notify("WARN", f"掘金双腿对照 {day} 有 {len(flags)} 项红旗：\n" +
                   "\n".join("- " + f for f in flags) + "\n\n" + report)
        else:
            notify("INFO", f"掘金双腿对照 {day} 一致 ✅\n" + report)
    return 1 if flags else 0


if __name__ == "__main__":
    raise SystemExit(main())
