# -*- coding: utf-8 -*-
"""掘金日终播报（QuanterEmquantEodReport · 钉钉对齐掘金 2026-08-28）。

每日 15:45（EOD 15:36 落盘后）推送掘金腿当日全景到钉钉——取代旧本地腿的
T+1 计划推送位（引擎 eod 已随 QMT 退役，其 76 单僵尸计划不再产生）：

  ① 当日漏斗：信号 N → 挂单 M（含钳价/回补标记）→ 成交 K → 拦截分布
     （定尺不足一手 / 单日上限 / 额度）逐类计数；
  ② 持仓表：7002 API 实时（symbol × qty × vwap × 浮盈）；
  ③ 资金面：nav / 可用 / 冻结（市值）；
  ④ EOD 摘要：audit 的 EOD 行（effective_today/open_orders/positions）。

数据源全部只读：audit CSV（策略目录）+ 7002 REST（Bearer=runtime.json）。
报告全文落 logs/emquant_eod_YYYY-MM-DD.txt（复盘留痕），钉钉 INFO 单条推送。
"""
from __future__ import annotations

import argparse
import csv
import json
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


def _audit_stats(day: str) -> dict:
    """当日 audit 漏斗统计（缺文件=全零，不报错——非交易日/停机日语义）。"""
    src = gc.audit_csv_path(day)
    st = {"signals": 0, "placed": 0, "fills": 0, "blocked": Counter(),
          "clamped": 0, "repaired": 0, "eod": None}
    if not src.exists():
        return st
    with src.open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 2 or not row[0].startswith(day):
                continue
            ev = row[1]
            if ev == "SIGNAL":
                st["signals"] += 1
            elif ev == "ORDER_PLACED":
                st["placed"] += 1
                try:
                    d = json.loads(row[2]) if len(row) > 2 else {}
                except ValueError:
                    d = {}
                if d.get("repair_of"):
                    st["repaired"] += 1
            elif ev == "POS_ENRICHED":
                st["fills"] += 1
            elif ev == "ORDER_BLOCKED":
                reason = (row[2] if len(row) > 2 else "")
                if "定尺不足一手" in reason:
                    st["blocked"]["定尺不足一手"] += 1
                elif "单日" in reason:
                    st["blocked"]["单日上限"] += 1
                elif "额度" in reason:
                    st["blocked"]["额度"] += 1
                else:
                    st["blocked"]["其他"] += 1
            elif ev == "WARN":
                try:
                    d = json.loads(row[2]) if len(row) > 2 else {}
                except ValueError:
                    d = {}
                if d.get("type") == "buy_price_clamped":
                    st["clamped"] += 1
            elif ev == "EOD":
                try:
                    st["eod"] = json.loads(row[2]) if len(row) > 2 else {}
                except ValueError:
                    pass
    return st


def _gm_symbol_to_ts(sym: str) -> str:
    ex, _, code = str(sym).partition(".")
    return f"{code}.{'SH' if ex == 'SHSE' else 'SZ' if ex == 'SZSE' else ex}"


def _api_snapshot(token: str, account_id: str) -> tuple[list, dict | None]:
    _, pos = gc.api_get(f"/v3/account-trade/positions/{account_id}", token)
    _, cash = gc.api_get(f"/v3/account-trade/cash/{account_id}", token)
    rows = []
    for p in (pos or {}).get("data") or []:
        if int(p.get("volume") or 0) > 0:
            rows.append({"sym": _gm_symbol_to_ts(p.get("symbol", "?")),
                         "qty": int(p["volume"]), "vwap": float(p.get("vwap") or 0),
                         "fpnl": float(p.get("fpnl") or 0)})
    cash_row = ((cash or {}).get("data") or [None])[0]
    return rows, cash_row


def build_report(now: datetime | None = None) -> str:
    now = now or datetime.now()
    day = f"{now:%Y-%m-%d}"
    cfg = gc.runtime_config()
    st = _audit_stats(day)
    positions, cash = _api_snapshot(str(cfg.get("token") or ""),
                                    str(cfg.get("account_id") or ""))
    lines = [f"掘金日终播报 · {day}"]
    blk = " / ".join(f"{k}×{v}" for k, v in st["blocked"].items()) or "无"
    lines.append(f"① 漏斗：信号 {st['signals']} → 挂单 {st['placed']}"
                 f"（回补 {st['repaired']}、钳价 {st['clamped']}）→ 成交 {st['fills']}"
                 f"｜拦截：{blk}")
    if positions:
        pos_txt = "；".join(f"{p['sym']}×{p['qty']}@{p['vwap']:.2f}"
                            f"({'+' if p['fpnl'] >= 0 else ''}{p['fpnl']:.0f})"
                            for p in positions)
        lines.append(f"② 持仓：{pos_txt}")
    else:
        lines.append("② 持仓：空仓")
    if cash:
        lines.append(f"③ 资金：nav {float(cash.get('nav') or 0):,.0f}"
                     f"｜可用 {float(cash.get('available') or 0):,.0f}"
                     f"｜市值 {float(cash.get('market_value') or 0):,.0f}")
    e = st["eod"] or {}
    if e:
        lines.append(f"④ EOD：effective_today={e.get('effective_today')}"
                     f"｜open_orders={e.get('open_orders')}"
                     f"｜placed_today={e.get('placed_today')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="掘金日终播报（钉钉对齐）")
    p.add_argument("--date", help="YYYY-MM-DD（缺省今天；回看历史留痕用）")
    p.add_argument("--no-push", action="store_true", help="只落文件不推钉钉")
    p.add_argument("--register", action="store_true", help="注册 15:45 每日 schtasks")
    p.add_argument("--unregister", action="store_true")
    args = p.parse_args(argv)
    if args.register or args.unregister:
        if args.register:
            py = ROOT / ".venv310" / "Scripts" / "python.exe"
            rc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "15:45",
                                 "/TN", "QuanterEmquantEodReport",
                                 "/TR", f'"{py}" "{Path(__file__)}"'],
                                capture_output=True).returncode
            print("registered" if rc == 0 else f"failed rc={rc}")
            return rc
        subprocess.run(["schtasks", "/Delete", "/TN", "QuanterEmquantEodReport", "/F"],
                       capture_output=True)
        print("unregistered")
        return 0

    now = datetime.now()
    report = build_report(now if not args.date else
                          datetime.strptime(args.date, "%Y-%m-%d"))
    print(report)
    log = ROOT / "logs" / f"emquant_eod_{(args.date or f'{now:%Y-%m-%d}')}.txt"
    log.parent.mkdir(exist_ok=True)
    log.write_text(report, encoding="utf-8")
    if not args.no_push:
        notify("INFO", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
