# -*- coding: utf-8 -*-
"""掘金日终播报（QuanterEmquantEodReport · 钉钉对齐掘金 2026-08-28）。

每日 15:45（EOD 15:36 落盘后）推送掘金腿当日全景到钉钉——取代旧本地腿的
T+1 计划推送位（引擎 eod 已随 QMT 退役，其 76 单僵尸计划不再产生）：

  ① 当日漏斗：信号 N → 挂单 M（含钳价/回补标记）→ 成交 K → 拦截分布
     （定尺不足一手 / 单日上限 / 额度）逐类计数；
  ② 持仓表：7002 API 实时（symbol × qty × vwap × 浮盈，按浮盈降序）；
  ③ 资金面：nav / 可用 / 冻结（市值）；
  ④ EOD 摘要：audit 的 EOD 行（effective_today/open_orders/positions）；
  ⑤ 明日预案（T+1 · 2026-09-01 新增"掘金侧计划→播报"桥，同日改**例外制**）：
     止损/止盈是定身位、隔日不变，逐只重列与 ② 同源=噪声（用户质询"为什么不和
     持仓去重"后修正）——本段只列「明日起变化/需动作」项：超期线/临近超期、
     TP1 已兑现目标切换、force_exit 标记；其余持仓一句话收口指向 ②。新信号
     扫描时点=次晨 09:31（GM 侧无盘前预产计划，明示不冒充预告——与旧本地腿
     "T 日 EOD 产 T+1 计划"的口径差异在此说明）。

排版：DingTalk markdown 约束（#/粗体/引用/列表，无表格无着色）——段标加粗、
关键数字加粗、止损止盈带距现价百分比，浮盈正负不着色靠符号+合计行锚定。

数据源全部只读：audit CSV（策略目录）+ 7002 REST（Bearer=runtime.json）
+ state.pkl（entry_date/止损止盈定身价）。
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


def _audit_stats(day: str, leg_dir: Path | None = None) -> dict:
    """当日 audit 漏斗统计（缺文件=全零，不报错——非交易日/停机日语义）。"""
    src = gc.audit_csv_path(day, leg_dir)
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


def _name_map(ts_symbols: list[str]) -> dict[str, str]:
    """ts 符号 → 中文公司名（data_lake/stock_basic.parquet 单源；缺行/读失败回退
    '—'——名字是展示层增强，绝不让它阻断日报主链）。"""
    try:
        import pandas as pd
        df = pd.read_parquet(ROOT / "data_lake" / "stock_basic.parquet",
                             columns=["ts_code", "name"])
        want = set(ts_symbols)
        sub = df[df["ts_code"].isin(want)]
        return dict(zip(sub["ts_code"], sub["name"]))
    except Exception:
        return {}


def _stop_tp_map(leg_dir: Path | None = None) -> dict[str, tuple]:
    """state.pkl 持仓 → (stop, tp1, tp2)（enrich 挂载的定终身价；读失败=空表降级）。"""
    m = _live_pos_map(leg_dir)
    return {s: (p.get("stop"), p.get("tp1_price"), p.get("tp2_price"))
            for s, p in m.items()}


def _live_pos_map(leg_dir: Path | None = None) -> dict[str, dict]:
    """state.pkl 活口持仓 → 完整 pos dict（stop/tp/entry_date/exec_params）。

    ⑤明日预案的数据源：entry_date 算持仓天数、exec_params.max_holding 判超期
    预警、tp1_done 判下一目标位。读失败=空表降级（预案段渲染"—"，不炸主链）。
    """
    try:
        st = json.loads(gc.state_pkl_path(leg_dir).read_text(encoding="utf-8"))
        return {sym: pos for sym, pos in (st.get("positions") or {}).items()
                if int((pos or {}).get("remaining_qty") or 0) > 0}
    except (OSError, ValueError):
        return {}


def _f(v, nd=2):
    """数值格式化；None/NaN → '—'（展示层绝不输出 None 字面量）。"""
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) and v == v else "—"


def _pct(target, last):
    """目标价距现价百分比（+x%/-x%）；任一侧缺价 → 空串（不硬造）。"""
    if not (isinstance(target, (int, float)) and isinstance(last, (int, float))
            and target == target and last == last and last > 0):
        return ""
    return f"（{target / last - 1:+.0%}）"


def _days_held(entry_date: str | None, day: str) -> int | None:
    """自然日持仓天数；entry_date 缺失/解析失败 → None。"""
    if not entry_date:
        return None
    try:
        from datetime import date as _date
        y, m, d = (int(x) for x in str(entry_date)[:10].split("-"))
        return (_date.fromisoformat(day) - _date(y, m, d)).days
    except (ValueError, TypeError):
        return None


def _api_snapshot(token: str, account_id: str) -> tuple[list, dict | None]:
    _, pos = gc.api_get(f"/v3/account-trade/positions/{account_id}", token)
    _, cash = gc.api_get(f"/v3/account-trade/cash/{account_id}", token)
    rows = []
    for p in (pos or {}).get("data") or []:
        if int(p.get("volume") or 0) > 0:
            rows.append({"sym": _gm_symbol_to_ts(p.get("symbol", "?")),
                         "qty": int(p["volume"]), "vwap": float(p.get("vwap") or 0),
                         "fpnl": float(p.get("fpnl") or 0),
                         "last": float(p.get("price") or 0)})
    cash_row = ((cash or {}).get("data") or [None])[0]
    return rows, cash_row


def build_report(now: datetime | None = None, leg_dir: Path | None = None,
                 leg_label: str = "主腿") -> str:
    """单腿日终报告（双腿形态 2026-08-28：main() 按 active_legs 循环；leg_dir=None
    走 gc.GM_STRATEGY_DIR 缺省——单腿调用方/既有测试语义不变）。

    2026-09-01 排版升级：DingTalk markdown（段标加粗/关键数字加粗/止损止盈带
    距现价百分比/持仓按浮盈降序+合计）+ 新增 ⑤明日预案段（掘金侧计划→播报桥）。
    """
    now = now or datetime.now()
    day = f"{now:%Y-%m-%d}"
    cfg = gc.runtime_config(leg_dir)
    st = _audit_stats(day, leg_dir)
    positions, cash = _api_snapshot(str(cfg.get("token") or ""),
                                    str(cfg.get("account_id") or ""))
    live = _live_pos_map(leg_dir)
    names = _name_map([p["sym"] for p in positions] + list(live))
    lines = [f"### 📊 掘金日终播报 · {day} · {leg_label}", ""]

    # ① 漏斗
    blk = " / ".join(f"{k}×{v}" for k, v in st["blocked"].items()) or "无"
    lines.append(f"**① 今日漏斗**：信号 {st['signals']} → 挂单 {st['placed']}"
                 f"（回补 {st['repaired']} · 钳价 {st['clamped']}）→ 成交 {st['fills']}"
                 f"｜拦截：{blk}")
    lines.append("")

    # ② 持仓快照（按浮盈降序；止损止盈带距现价 %）
    if positions:
        rows = sorted(positions, key=lambda p: p["fpnl"], reverse=True)
        total = sum(p["fpnl"] for p in rows)
        lines.append(f"**② 持仓快照**（{len(rows)} 只 · 浮盈合计 "
                     f"**{'+' if total >= 0 else ''}{total:,.0f}**）")
        for p in rows:
            stop, tp1, tp2 = (live.get(p["sym"]) or {}).get("stop"), \
                (live.get(p["sym"]) or {}).get("tp1_price"), \
                (live.get(p["sym"]) or {}).get("tp2_price")
            lines.append(
                f"- **{names.get(p['sym'], '—')}** {p['sym']} ×{p['qty']}"
                f"｜成本 {_f(p['vwap'])} → 现 {_f(p['last'])}"
                f"｜**{'+' if p['fpnl'] >= 0 else ''}{p['fpnl']:.0f}**"
                f"｜止损 {_f(stop)}{_pct(stop, p['last'])}"
                f"｜TP1 {_f(tp1)}{_pct(tp1, p['last'])} / TP2 {_f(tp2)}{_pct(tp2, p['last'])}")
    else:
        lines.append("**② 持仓快照**：空仓")
    lines.append("")

    # ③ 资金面
    if cash:
        lines.append(f"**③ 资金面**：nav **{float(cash.get('nav') or 0):,.0f}**"
                     f"｜可用 {float(cash.get('available') or 0):,.0f}"
                     f"｜市值 {float(cash.get('market_value') or 0):,.0f}")
        lines.append("")

    # ④ EOD 摘要
    e = st["eod"] or {}
    if e:
        lines.append(f"**④ EOD 摘要**：effective_today={e.get('effective_today')}"
                     f"｜open_orders={e.get('open_orders')}"
                     f"｜placed_today={e.get('placed_today')}")
        lines.append("")

    # ⑤ 明日预案（T+1）：例外制（2026-09-01 用户质询"为什么不和持仓去重"后的
    # 信息架构修正）——止损/止盈是 entry 挂载的定身位，隔日不变（trailing 活态
    # 才随价上移），逐只重复渲染与 ② 完全同源=噪声。故 ⑤ 只列「明日起变化/需
    # 动作」项：超期线/临近超期（尾盘 force_exit）、TP1 已兑现（上望切 TP2）、
    # force_exit 标记；其余持仓一句话收口指向 ② 的定身位。新信号扫描在次晨
    # 09:31——GM 侧无盘前预产计划，明示时点不冒充预告（次晨 09:40 晨检为准确认）。
    lines.append("**⑤ 明日预案（T+1）**")
    lines.append("- 新信号：明早 09:31 盘前扫描自动挂单（详见 09:40 晨检「今日计划」段）")
    if live:
        lasts = {p["sym"]: p["last"] for p in positions}
        exceptions: list[tuple[int, str]] = []
        quiet = 0
        for sym, pos in live.items():
            days = _days_held(pos.get("entry_date"), day)
            mh = int((pos.get("exec_params") or {}).get("max_holding") or 0)
            last = lasts.get(sym)
            bits = []
            if days is not None and mh:
                if days >= mh:
                    bits.append(f"第{days}/{mh}天 **已到超期线**——明日尾盘超期平仓")
                elif days >= mh - 3:
                    bits.append(f"第{days}/{mh}天 临近超期")
            # 止盈档进度（regime 感知，2026-09-01 修向）：现役 R6-8 是反转 regime
            # （tp1=2H > tp2=1.5H）——先触 TP2 卖 lot2（10%），主仓上望 TP1（2H）；
            # tp1_done 在反转 regime=清仓（remaining=0，不会出现在持仓里），此分支
            # 仅正常 regime（tp1<tp2，历史档）可达。方向按价格序判，不猜 regime。
            tp1, tp2 = pos.get("tp1_price"), pos.get("tp2_price")
            inverted = (tp1 is not None and tp2 is not None and float(tp1) > float(tp2))
            if inverted and pos.get("tp2_done") and not pos.get("tp1_done"):
                bits.append(f"TP2 已兑（lot2 落袋），主仓上望 TP1 {_f(tp1)}{_pct(tp1, last)}")
            elif not inverted and pos.get("tp1_done"):
                bits.append(f"TP1 已兑现，上望目标切 TP2 {_f(tp2)}{_pct(tp2, last)}")
            if pos.get("force_exit"):
                bits.append("force_exit 标记在场")
            if bits:
                exceptions.append((-(days if days is not None else 0),
                                    f"- ⚠️ **{names.get(sym, '—')}** {sym}："
                                    + "；".join(bits)))
            else:
                quiet += 1
        lines.extend(ln for _, ln in sorted(exceptions, key=lambda x: x[0]))
        if quiet:
            lines.append(f"- 其余 {quiet} 只按 ② 定身位继续执行"
                         f"（止损/TP 见上；trailing 活态下止损随价上移）")
    else:
        lines.append("- 持仓管理：无持仓")
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
            # W0（0828 评审）：/TR 走 run_ops_task.bat 统一包装器（输出重定向防启动期静默）
            bat = Path(__file__).parent / "run_ops_task.bat"
            rc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "15:45",
                                 "/TN", "QuanterEmquantEodReport",
                                 "/TR", f'"{bat}" emquant_eod_report.py'],
                                capture_output=True).returncode
            print("registered" if rc == 0 else f"failed rc={rc}")
            return rc
        subprocess.run(["schtasks", "/Delete", "/TN", "QuanterEmquantEodReport", "/F"],
                       capture_output=True)
        print("unregistered")
        return 0

    now = datetime.now()
    # W3（2026-08-28 评审 P3）：交易日闸——周末 15:45 schtasks 照触发会推
    # 「信号 0 → 挂单 0 → 成交 0」全零播报（_audit_stats 缺文件返全零是明示设计，
    # 但周末推全零是噪声）。显式 --date 回看不受闸（人工意图优先）。
    if not args.date and now.weekday() >= 5:
        notify("INFO", f"掘金EOD {now:%Y-%m-%d} 非交易日（周末），跳过日终播报——schtask 心跳正常")
        return 0
    # 双腿形态（2026-08-28 双轨 §4.3）：逐腿出报告，双腿拼一条钉钉（拆两条会被
    # 群折叠语义拆散对照关系）；报告文件按腿分文件落档。
    day_str = args.date or f"{now:%Y-%m-%d}"
    parts = []
    for leg in gc.active_legs():
        report = build_report(now if not args.date else datetime.strptime(args.date, "%Y-%m-%d"),
                              gc.leg_strategy_dir(leg), leg.label)
        parts.append(report)
        log = ROOT / "logs" / f"emquant_eod_{leg.key}_{day_str}.txt"
        log.parent.mkdir(exist_ok=True)
        log.write_text(report, encoding="utf-8")
    full = "\n\n".join(parts)
    print(full)
    if not args.no_push:
        notify("INFO", full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
