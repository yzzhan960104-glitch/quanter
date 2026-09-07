# -*- coding: utf-8 -*-
"""掘金侧次日计划预演（2026-09-01 · 「掘金侧计划→播报」桥第三块）。

物理意图：GM 侧唯一计划机制=次晨 09:31 pre_open 现扫现挂（15:45 EOD 时点无预产
计划工件，湖也停在 T-1）——本脚本用【湖前复权数据 + 实盘 state.pkl + 7002 资金】
离线复刻同一识别链（importlib 加载终端同版产物），回答"以当前参数与持仓，下一
交易日 09:31 会挂什么单"。与桥另两块合璧：EOD 15:45 ⑤明日预案（例外制）→
【本脚本·预演】→ 次晨 09:40 晨检 ⑦今日计划（实况确认）。

复刻环（与 pre_open ③④④'⑤ 严格同序，importlib 单源）：
  UNIVERSE 扫描 detect_signal（湖截至 data_day）→ cooldown 锚点去重（当前快照
  cooldown=0=no-op，>0 时以自然日近似并留痕）→ held/在途买去重（SIGNAL_SKIP_HELD
  同口径）→ RISK_BLOCK 人工旗 → amihud keep-top5（湖 close/amount 现算同公式：
  |日收益|/成交额 window 根均值，≥min_days 才有效）→ 钳涨停带（limit_up_price
  自算档；实跑 API 值优先）→ 7.5% 定尺（equity=7002 nav）→ 单日闸5 → 整手拦截。

与实跑的已知偏差（预演≠计划，08-31 对拍实证识别层逐字段一致）：
  差异只来自闸序运行时状态——预演时点与次晨间的 state 变化（今晨成交/止损/人工
  干预）、涨停价 API 值 vs 自算档、RISK_BLOCK 中途 touch。播报恒带「预演」标签。

时序红线：仅在 18:00 管道把 T 日数据落湖后跑才有完整视野（data_day=湖内最新收盘）。
用法：python -m ops.emquant_plan_preview [--no-push]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

PILOT = ROOT / "emquant" / "emquant_neckline_pilot.py"


def balanced_guards(data_day: str, plan: list, index: str = "399006.SZ",
                    ma_window: int = 60, chase_skip_atr: float = 3.0) -> dict:
    """均衡栈守卫预判（每日回顾页「明日计划」附件 · 2026-09-07）。

    与部署产物 market_throttle_blocked / CHASE_SKIP_ATR 同口径，但从湖算
    （预演时点 T=data_day 收盘，实盘 pre_open 用 gm history 拉同一根 K 线）：
    - t1：指数（创业板指）data_day 收盘 vs MA60 → 均衡栈腿明日是否停新仓；
    - rows：逐单挂单溢价（entry−neckline）/ATR 估值——预演行无 atr，用
      buy_limit_atr_mult=2.5 反推（挂单价=颈线+2.5ATR ⇒ 溢价恒 2.5，帽内），
      skip@3.0 只在 chase 追入时生效，此处仅作提示。
    fail-open：湖缺数据 → t1=None（页面显示「不可判」，不伪造）。
    """
    out = {"index": index, "ma_window": ma_window, "chase_skip_atr": chase_skip_atr,
           "t1": None, "rows": []}
    try:
        import pandas as pd
        idx = pd.read_parquet(ROOT / "data_lake" / "index_daily.parquet", columns=["close"])
        code, ex = index.split(".")
        gm_sym = ("SZSE." if ex == "SZ" else "SHSE.") + code     # 湖可能是 ts 或 gm 两种符号口径
        try:
            s = idx.xs(index, level="symbol")["close"].sort_index()
        except KeyError:
            s = idx.xs(gm_sym, level="symbol")["close"].sort_index()
        s = s[s.index <= pd.Timestamp(data_day)]
        if len(s) >= ma_window:
            close, ma = float(s.iloc[-1]), float(s.iloc[-ma_window:].mean())
            out["t1"] = {"close": round(close, 2), "ma": round(ma, 2),
                         "ratio": round(close / ma, 4), "blocked": close < ma,
                         "as_of": str(s.index[-1].date()),
                         "balanced_action": "明日停新仓（T1 大盘节流命中：指数<MA60）"
                         if close < ma else "正常挂单（指数≥MA60）"}
    except Exception as e:  # noqa: BLE001 —— 守卫预判失败不阻断预演
        out["t1_error"] = f"{type(e).__name__}: {e}"[:120]
    for r in plan or []:
        try:
            entry, neck = float(r.get("entry") or 0), float(r.get("neckline") or 0)
            atr_est = (entry - neck) / 2.5 if entry > neck else None
            prem = round((entry - neck) / atr_est, 2) if atr_est else None
            out["rows"].append({"sym": r.get("sym"), "premium_atr_est": prem,
                                "chase_skip_note": f"挂单溢价≈{prem}ATR（帽内）；仅 chase 追入超 "
                                                   f"{chase_skip_atr}ATR 时弃单" if prem else "几何缺失"})
        except (TypeError, ValueError, ZeroDivisionError):
            out["rows"].append({"sym": r.get("sym"), "premium_atr_est": None})
    return out


def attach_committee_review(path: Path, tier: str = "A") -> dict:
    """给预演档案追加 committee_review + guards（幂等：重跑覆盖两键，其余不动）。

    评审对象=预演正文（信号→挂单→拦截漏斗 + 逐单几何）+ 守卫预判；kind=
    plan_preview_{plan_date}（产物落 logs/committee_reviews/）。fail-open：
    委员会不可用 → verdict=UNAVAILABLE 仍落键（页面显示「评审不可用」而非缺块）。
    与 loser_review 的 committee_review 同形状（verdict/kb_conflicts/evidence_grade/
    notes/llm_calls/artifact/disclaimer），前端一套渲染。
    """
    art = json.loads(Path(path).read_text(encoding="utf-8"))
    guards = balanced_guards(str(art.get("data_day") or ""), art.get("plan") or [])
    art["guards"] = guards
    rows_txt = "\n".join(
        f"- {r.get('sym')} qty={r.get('qty')} 挂单价={r.get('entry')} 颈线={r.get('neckline')} "
        f"rr={r.get('rr')} 形成日={r.get('formed')}" for r in (art.get("plan") or [])) or "- （无计划单）"
    t1 = guards.get("t1") or {}
    doc = (f"# 明日计划预演 {art.get('plan_date')}（数据日 {art.get('data_day')}）\n"
           f"版本锚 {art.get('stamp')}；权益 {art.get('equity')}；信号 {art.get('n_signals')} → "
           f"计划 {art.get('n_plan')}（拦截 {art.get('n_blocked')}，amihud 剔除 "
           f"{len(art.get('dropped_amihud') or [])}，已持有跳过 {len(art.get('skip_held') or [])}）\n\n"
           f"## 计划单\n{rows_txt}\n\n"
           f"## 拦截明细\n" + ("\n".join(f"- {b.get('sym')}: {b.get('why')}" for b in (art.get('blocked') or [])) or "- 无") +
           f"\n\n## 均衡栈守卫预判\nT1 大盘节流：{t1.get('balanced_action', '不可判')}"
           f"（{guards.get('index')} 收盘 {t1.get('close')} vs MA60 {t1.get('ma')}，ratio {t1.get('ratio')}）\n"
           f"追价守卫 skip@{guards.get('chase_skip_atr')}：挂单价溢价恒 2.5ATR 帽内，仅 chase 追入触发。\n\n"
           f"请以知识库（regime_dependence/throttle_design/filters_7waves/pertrade_calibers）"
           f"核查：①明日是否处于策略期望为负的行情状态；②计划单的形态几何/rr 是否触及已否决"
           f"形状；③两腿（主腿无守卫 vs 均衡栈腿 T1+skip）明日行为差异是否合理。"
           f"只做事实核查与风险标注，不给交易指令。")
    try:
        from research.committee.review import review_conclusion
        cr = review_conclusion(f"plan_preview_{art.get('plan_date')}", doc, tier=tier,
                               subject={"plan_date": art.get("plan_date"), "n_plan": art.get("n_plan"),
                                        "guards": guards})
    except Exception as e:  # noqa: BLE001
        cr = {"verdict": "UNAVAILABLE", "kb_conflicts": [], "evidence_grade": "",
              "notes": f"{type(e).__name__}: {e}"[:200], "llm_calls": 0, "artifact": None}
    cr.pop("transcripts", None)                        # 档案瘦身：完整 transcript 在 committee_reviews 产物
    if len(json.dumps(cr.get("fact_checks") or [], ensure_ascii=False)) > 4000:
        cr.pop("fact_checks", None)
    art["committee_review"] = cr
    Path(path).write_text(json.dumps(art, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return {"verdict": cr.get("verdict"), "t1": t1.get("balanced_action"), "llm_calls": cr.get("llm_calls")}


def load_product():
    """importlib 加载掘金产物（dataclass PEP563：先注册 sys.modules 再 exec）。"""
    spec = importlib.util.spec_from_file_location("pilot_artifact_preview", PILOT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["pilot_artifact_preview"] = m
    spec.loader.exec_module(m)
    return m


def amihud_from_frame(sub, window: int, min_days: int):
    """湖版 amihud60：|日收益|/成交额 在 window 根内的均值（≥min_days 才有效）。

    与产物 fetch_amihud60 同口径（close 前复权 / amount 原始千元 / 悬停日天然
    缺行=skip_suspended 等价）；sub 需含 close/amount 两列、按日升序。
    """
    try:
        w = sub.tail(window + 1)
        ret = w["close"].astype(float).pct_change()
        amt = w["amount"].astype(float)
        valid = ret.notna() & amt.notna() & (amt > 0)
        n = int(valid.iloc[1:].sum())          # 首行 ret 恒 NaN 不计
        if n < min_days:
            return None
        v = (ret[valid] / amt[valid]).abs().mean()
        return float(v) if v == v else None
    except Exception:
        return None


def build_preview(m, *, no_push: bool = False) -> tuple[str, dict]:
    """组装预演（主腿）。返回 (markdown, 摘要 dict)；纯读零写。"""
    import pandas as pd

    from ops import gm_ops_common as gc
    from ops.emquant_eod_report import _name_map

    lake = pd.read_parquet(ROOT / "data_lake" / "a_shares_daily.parquet")
    data_day = pd.Timestamp(lake.index.get_level_values("date").max())
    cutoff = data_day - pd.Timedelta(days=520)     # 160 识别根 + 60 amihud 根的日历裕量
    recent = lake[lake.index.get_level_values("date") >= cutoff]
    frames = {sym: g for sym, g in recent.groupby(level="symbol")}

    state = json.loads(gc.state_pkl_path().read_text(encoding="utf-8"))
    held = {s for s, p in (state.get("positions") or {}).items()
            if int((p or {}).get("remaining_qty") or 0) > 0}
    open_buy = {o.get("symbol") for o in (state.get("orders") or {}).values()
                if o.get("symbol")
                and o.get("purpose") in m._BUY_AMOUNT_PURPOSES
                and o.get("status") not in m._TERMINAL_ORDER_STATES}

    cfg = gc.runtime_config()
    token, acc = str(cfg.get("token") or ""), str(cfg.get("account_id") or "")
    st_cash, cash = gc.api_get(f"/v3/account-trade/cash/{acc}", token)
    equity = float(((cash or {}).get("data") or [{}])[0].get("nav") or 0) \
        if st_cash == 200 else None

    window = int(m.ID_PARAMS["window"])
    n_roots = 2 * window + 40
    cooldown = int(m.EXEC_PARAMS["cooldown"])
    skip_held, scan_fail = [], 0
    signals = []
    for sym in m.UNIVERSE:
        g = frames.get(sym)
        if g is None:
            continue
        try:
            df = g.xs(sym, level="symbol").sort_index()
            df = df.loc[:data_day, ["open", "high", "low", "close", "volume"]] \
                   .tail(n_roots).copy()
            df.index = pd.DatetimeIndex(pd.to_datetime(df.index)).normalize()
            sig = m.detect_signal(sym, df, m.ID_PARAMS, m.EXEC_PARAMS,
                                  data_day.strftime("%Y-%m-%d"))
        except Exception:
            scan_fail += 1
            continue
        if sig is None:
            continue
        if cooldown > 0:   # 当前快照 cooldown=0；>0 时自然日近似留痕（诚实降级）
            last = (state.get("last_signal") or {}).get(sym)
            if last is not None and (
                    (data_day - pd.Timestamp(last)).days < cooldown * 1.45):
                skip_held.append((sym, "cooldown"))
                continue
        if sym in held or sym in open_buy:
            skip_held.append((sym, "held" if sym in held else "open_buy"))
            continue
        signals.append(sig)

    blocked_flag = m.is_blocked()
    if blocked_flag:
        signals = []

    af = m.AMIHUD_FILTER
    dropped_amihud: list = []
    if signals and af.get("enabled"):
        vals = {}
        for s in signals:
            g = frames.get(s.symbol)
            vals[s.symbol] = amihud_from_frame(
                g.xs(s.symbol, level="symbol")[["close", "amount"]],
                int(af["window"]), int(af["min_days"])) if g is not None else None
        signals, _dropped, _exempt = m.apply_amihud_filter(
            signals, vals, int(af["keep_top"]))
        dropped_amihud = [s for s, _v in _dropped]   # (sym, amihud60) 元组拆包

    pos_cap = float(m.TRADE_CFG.get("pos_cap", 0.075))
    cap = int(m.PILOT_MAX_NEW_ORDERS_PER_DAY)
    closes = {sym: float(frames[sym].xs(sym, level="symbol")
                         .sort_index()["close"].iloc[-1])
              for sym in {s.symbol for s in signals} if sym in frames}
    plan, blocked = [], []
    n_eff = 0
    for s in signals:
        entry = float(s.entry_price)
        prev_close = closes.get(s.symbol)
        upper = m.limit_up_price(prev_close, s.symbol) if prev_close else None
        clamped = bool(upper and entry > upper)
        if clamped:
            entry = upper
        if equity:
            qty = int(equity * pos_cap / entry / 100) * 100
        else:
            qty = 0
        minq = m._min_order_qty(s.symbol)   # 板块感知（科创板≥200，与策略 2026-09-01 同口径）
        if equity is None:
            blocked.append((s.symbol, "资金不可得（7002）"))
        elif qty < minq:
            blocked.append((s.symbol, f"定尺不足最小申报量"
                            f"（{equity:.0f}×{pos_cap:g}={equity * pos_cap:.0f}"
                            f" < {minq}×{entry:.2f}；科创板≥200/其余≥100）"))
        elif n_eff >= cap:
            blocked.append((s.symbol, f"单日上限 {cap}"))
        else:
            n_eff += 1
            plan.append(dict(sym=s.symbol, qty=qty, entry=entry,
                             clamped=clamped, neckline=float(s.neckline),
                             rr=float(s.rr),
                             formed=str(s.formed_at)[:10] if s.formed_at else "—"))

    names = _name_map([p["sym"] for p in plan] + [s for s, _ in blocked]
                      + list(dropped_amihud) + [s for s, _ in skip_held])
    day = data_day.strftime("%Y-%m-%d")
    lines = [f"### 🔮 次日计划预演（{day} 数据 → 次一交易日 09:31）", "",
             "> 预演 ≠ 计划：识别层与实跑同源逐字段一致；差异仅来自运行时闸态"
             "（预演后的成交/止损/人工干预、涨跌停 API 值）。权威以 09:40 晨检"
             "「今日计划」段为准。", ""]
    if blocked_flag:
        lines.append("**RISK_BLOCK 人工旗在场：明日增量挂单将被整体拦截**")
        lines.append("")
    if plan:
        budget = equity * pos_cap if equity else 0
        lines.append(f"**拟挂 {len(plan)} 单**（资金 {equity:,.0f} × 7.5% ≈ "
                     f"{budget:,.0f}/单）")
        for p in plan:
            lines.append(
                f"- **{names.get(p['sym'], '—')}** {p['sym']} ×{p['qty']}"
                f" @ {p['entry']:.2f}{'（钳涨停）' if p['clamped'] else ''}"
                f"｜颈线 {p['neckline']:.2f} · RR {p['rr']:.1f}"
                f"｜形态日 {p['formed']}")
    else:
        lines.append("**拟挂 0 单**（无候选或全被闸拦）")
    lines.append("")
    if blocked or dropped_amihud or skip_held:
        lines.append("**被闸剔除**")
        for sym, why in blocked:
            lines.append(f"- {names.get(sym, '—')} {sym}：{why}")
        for sym in dropped_amihud:
            lines.append(f"- {names.get(sym, '—')} {sym}：amihud keep-top5 剔除")
        for sym, why in skip_held:
            lines.append(f"- {names.get(sym, '—')} {sym}：{'已持有/在途' if why != 'cooldown' else '冷却期'}跳过")
        lines.append("")
    if scan_fail:
        lines.append(f"- ⚠️ {scan_fail} 只标的扫描异常跳过（挡板语义，不影响其余）")
    md = "\n".join(lines)
    summary = dict(data_day=day, n_signals=len(signals), n_plan=len(plan),
                   n_blocked=len(blocked), equity=equity)
    # 档案落盘（逻辑对齐机制的一半）：今晨晨检 ⑦' 预演对拍读这份档案与实挂逐单
    # 比对——把"预演准确性"从一次性对拍升级为每日实证。文件名=计划日（对拍侧
    # 按 today 直查）；--no-push 也落盘（回填口径）。
    try:
        from trading.calendar import next_trading_day
        plan_date = next_trading_day(day)
        art = {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
               "stamp": m.PILOT_BUILD_STAMP, "data_day": day, "plan_date": plan_date,
               "equity": equity, **summary,
               "plan": plan, "dropped_amihud": dropped_amihud,
               "skip_held": [{"sym": s, "why": w} for s, w in skip_held],
               "blocked": [{"sym": s, "why": w} for s, w in blocked],
               "risk_block": bool(blocked_flag)}
        path = ROOT / "logs" / f"plan_preview_{plan_date}.json"
        path.write_text(json.dumps(art, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        print(f"[artifact] 档案已落 {path}")
        # 每日回顾页附件（2026-09-07）：委员会评审 + 均衡栈守卫预判追加进同一档案
        # （fail-open：评审不可用也落 UNAVAILABLE 键，页面显示「不可用」而非缺块）。
        try:
            print(f"[committee] {attach_committee_review(path)}")
        except Exception as e:  # noqa: BLE001 —— 评审失败不阻断预演主链
            print(f"[committee] 评审追加失败（不阻断）：{type(e).__name__}: {e}")
    except Exception as e:   # 档案失败不阻断推送主链（对拍侧按缺档案降级）
        print(f"[artifact] 档案落盘失败（不阻断）：{type(e).__name__}: {e}")
    if not no_push:
        from ops.gm_ops_common import notify
        notify("INFO", md)
    return md, summary


def main(argv: list[str] | None = None) -> int:
    from datetime import datetime
    p = argparse.ArgumentParser(description="掘金侧次日计划预演（湖+state+7002 离线复刻）")
    p.add_argument("--no-push", action="store_true", help="只打印不推钉钉")
    args = p.parse_args(argv)
    # 周末闸：湖停在周五 → 预演与周五晚完全同稿，重推=噪声（人工 --no-push 回看不受限）
    if datetime.now().weekday() >= 5 and not args.no_push:
        print("[skip] 周末湖数据未前进，预演与周五同稿，跳过推送（--no-push 可本地回看）")
        return 0
    m = load_product()
    print(f"[artifact] stamp={m.PILOT_BUILD_STAMP}")
    md, summary = build_preview(m, no_push=args.no_push)
    print(md)
    print(f"\n[summary] {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
