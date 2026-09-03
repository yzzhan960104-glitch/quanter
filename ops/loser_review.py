# -*- coding: utf-8 -*-
"""每日亏损持仓 LLM 深度归因（loser review · 2026-09-03 用户需求）。

物理意图：每个交易日盘后（18:05 cron，避开 18:00-18:02 管道写湖窗口），
对双腿浮亏持仓（7002 fpnl<0 ∩ state 活仓）逐只做大模型深度归因——
为什么亏、亏在哪个环节（入场时机/止损设置/市场系统性/个股趋势/持有超期）、
当前处于什么风险状态。产物 logs/loser_review_{day}.json（双腿结构化）+
job_run 台账；public_snapshot._loser_review 读当日产物上站。

红线（只读分析的边界）：
  - 输入全部为持仓/行情**事实**（state/audit/湖/K线），无任何未来计划——零前跑面
  - 输出是归因观点与风险状态**描述**（持有观察/收紧关注/临近风控线三档），
    绝不产出交易指令、参数修改建议执行链——策略参数只属于研究线的提案流程
  - LLM 走 infra/llm 现成端口（z.ai glm-5.3），失败降级 analysis.error 不炸任务

调用方惯例：python -m ops.loser_review [--force]（幂等：重跑覆盖当日文件）。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

OUT_DIR = ROOT / "logs"
JOB_NAME = "ops_loser_review"

# 归因输出契约（prompt 内强制；解析失败降级保留原文 markdown）
_RISK_STATES = ("持有观察", "收紧关注", "临近风控线")

_PROMPT_TMPL = """你是一名资深量化策略风控官，管理一套 A 股颈线法动量策略（颈线突破入场，
trailing 止损随持有天数逐日收紧，TP 分档止盈，超期强平）。以下是当前一只**浮亏持仓**的全部
事实数据。请做深度亏损归因分析。

## 持仓事实
- 标的：{name}（{symbol}）· 行业：{industry} · 腿：{leg_label}
- 进场：{entry_date} @ 成本 {entry_price}（信号颈线 {neckline}，形态日 {formed_at}，
  信号时 ATR {signal_atr}，理论委托价 {entry_theory}，信号 RR {rr}）
- 现状：现价 {last}，浮亏 {fpnl} 元（{fpnl_pct}%），已持有 {days_held} 个交易日
  （max_holding {max_holding}，预计超期日 {expire_date}）
- 定身位：止损 {stop}（距现价 {dist_stop_pct}%）｜TP1 {tp1}｜TP2 {tp2}
- trailing：颈线锚 {t_neckline}，ATR {t_atr}，grace {grace} 天
  （{grace_state}，已收紧 {steps_taken} 步 × step {step}）
- 进场以来行情：最高 {high} / 最低 {low}（现价距进场后高点回撤 {dd_pct}%）
- 同期市场：上证 {sh_pct}%，沪深300 {hs300_pct}%

## 进场以来行情（压缩口径：背景=进场前 20 日收盘；进场后=逐日 收盘/涨跌%
与当日振幅；量能=进场后均量 vs 背景均量倍数）
{klines}

## 分析要求（输出两部分）
第一部分：严格 JSON（一个代码块）：
{{"primary": "主因（一句话，从：市场系统性下跌/个股趋势反转/入场时机偏晚/止损设置偏松/入场即逆风/流动性冲击/持有超期中选或自拟更准的）",
  "secondary": "次因（一句话）",
  "evidence": "量化证据（引用上面的数字）",
  "confidence": "高|中|低",
  "risk_state": "持有观察|收紧关注|临近风控线",
  "param_directions": {{"参数名": "建议研究线探索的方向（如 收紧至1.0-1.5 / 放宽 / 维持）"}}}}
其中 param_directions 是**研究探索提示**（喂给离线回测探索环的参数方向
假设，0-2 个键，从 stop_atr_mult/min_rr/max_holding/buy_limit_atr_mult/
breakout_vol_mult/max_holding/momentum_gate 中选）——不是参数修改指令，
线上参数的演进只走研究提案流的人审闸。
第二部分：中文 markdown 深度分析（300-600 字），小节：①亏损过程复盘（从进场日
到现在发生了什么）②归因（主次因展开，对照策略机制：为什么止损没触发/TP 遥远/
grace 期保护）③当前风险状态判定依据（距止损/超期/trailing 收紧进度的量化距离）。
只做归因、状态描述与探索方向提示，绝不给出买卖指令。"""


# ─────────────────────── 数据组装（纯函数，可测） ───────────────────────

def _fmt(v, nd=2):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _signal_of(leg_dir: Path, sym: str) -> dict:
    """audit 回扫该持仓的 SIGNAL 行（45 天窗口，最新一条胜出）——信号快照。"""
    from ops import gm_ops_common as gc
    from datetime import timedelta as _td
    hits: dict = {}
    for back in range(45):
        day = f"{datetime.now() - _td(days=back):%Y-%m-%d}"
        src = gc.audit_csv_path(day, leg_dir)
        if not src.exists():
            continue
        try:
            for row in csv.reader(src.open(encoding="utf-8")):
                if len(row) < 3 or row[1] != "SIGNAL":
                    continue
                try:
                    d = json.loads(row[2])
                except ValueError:
                    continue
                if d.get("symbol") == sym:
                    hits = d                      # 新→旧扫，先见=最新
        except OSError:
            continue
        if hits:
            break
    return hits or {}


def _market_pct(index_code: str, entry_date: str) -> float | None:
    """进场日收盘 → 最新收盘的指数涨跌 %（湖 index_daily）。"""
    import pandas as pd
    try:
        df = pd.read_parquet(ROOT / "data_lake" / "index_daily.parquet",
                             columns=["close"])
        sub = df.xs(index_code, level="symbol")["close"].sort_index()
        after = sub.loc[entry_date:]
        if len(after) < 2:
            return None
        return round((float(after.iloc[-1]) / float(after.iloc[0]) - 1) * 100, 2)
    except (KeyError, OSError):
        return None


def _klines_for(sym: str, entry_date: str, before: int = 20) -> tuple[list, dict, int | None]:
    """进场前 N 根背景 + 进场后全部日 K + 进场后高低/回撤统计（湖前复权）。

    返回 (rows, stat, entry_idx)：entry_idx=进场景在 rows 中的下标（渲染分界
    锚；entry 非交易日=None → 全窗口按背景压缩+尾部 10 根展开兜底）。
    """
    import pandas as pd
    df = pd.read_parquet(ROOT / "data_lake" / "a_shares_daily.parquet",
                         columns=["open", "high", "low", "close", "volume"])
    sub = df.xs(sym, level="symbol").sort_index()
    idx = sub.index.get_loc(entry_date) if entry_date in sub.index else None
    if idx is None:                               # 进场日停牌（脏数据）→ 最近 40 根
        window = sub.tail(40)
        since = window
    else:
        window = sub.iloc[max(0, idx - before):]  # 进场前 N 根 + 进场后**全部**
        since = sub.iloc[idx:]
    rows = [[f"{d:%Y-%m-%d}", round(float(o), 2), round(float(h), 2),
             round(float(l), 2), round(float(c), 2), int(v)]
            for d, o, h, l, c, v in zip(window.index, window["open"], window["high"],
                                        window["low"], window["close"], window["volume"])]
    entry_close = float(since["close"].iloc[0])
    hi, lo = float(since["high"].max()), float(since["low"].min())
    last = float(since["close"].iloc[-1])
    stat = {"high": round(hi, 2), "low": round(lo, 2),
            "dd_pct": round((last / hi - 1) * 100, 1) if hi > 0 else None,
            "since_entry_pct": round((last / entry_close - 1) * 100, 1)}
    offset = (idx - max(0, idx - before)) if idx is not None else None
    return rows, stat, offset


def build_context() -> list[dict]:
    """双腿浮亏持仓 → 每只一个上下文 dict（纯事实，无 LLM）。

    筛选：7002 positions fpnl<0 且 volume>0 ∩ state.pkl remaining_qty>0。
    """
    import bisect
    import pandas as pd
    from ops import gm_ops_common as gc
    from ops.public_snapshot import (_industry_map, _sh_calendar,
                                     _expire_date)

    cal = _sh_calendar()
    imap = _industry_map()
    token = str(gc.runtime_config().get("token") or "")
    out: list[dict] = []
    for leg in gc.active_legs():
        leg_dir = gc.leg_strategy_dir(leg)
        cfg = gc.runtime_config(leg_dir)
        lt = str(cfg.get("token") or token)
        la = str(cfg.get("account_id") or "")
        st, payload = gc.api_get(f"/v3/account-trade/positions/{la}", lt, timeout=4.0)
        live = {r.get("symbol"): r for r in ((payload or {}).get("data") or [])
                if isinstance(r, dict) and (r.get("fpnl") or 0) < 0
                and int(r.get("volume") or 0) > 0} if st == 200 else {}
        if not live:
            continue
        try:
            state = json.loads(gc.state_pkl_path(leg_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        for pos_sym, pos in (state.get("positions") or {}).items():
            if int((pos or {}).get("remaining_qty") or 0) <= 0:
                continue
            # state 键=ts 口径 300433.SZ（code 在前）→ gm 口径 SZSE.300433
            code, _, ex = str(pos_sym).partition(".")
            gm_sym = f"{'SHSE' if ex == 'SH' else 'SZSE'}.{code}"
            row = live.get(gm_sym)
            if not row:
                continue
            entry_date = str(pos.get("entry_date") or "")
            max_hold = int((pos.get("exec_params") or {}).get("max_holding") or 30)
            tr = pos.get("trailing") or {}
            # 持有交易日数 = (entry, today]（与策略 trading_days_between 同式）
            days_held = (bisect.bisect(cal, f"{datetime.now():%Y-%m-%d}")
                         - bisect.bisect(cal, entry_date)) if entry_date in cal else None
            sig = _signal_of(leg_dir, pos_sym)
            try:
                klines, kstat, k_entry_idx = _klines_for(pos_sym, entry_date)
            except KeyError:
                klines, kstat, k_entry_idx = [], {}, None
            ts_sym = pos_sym                        # state 键已是 ts 口径
            last = float(row.get("last_price") or row.get("price") or 0)
            stop = pos.get("stop")
            grace = int(tr.get("grace") or 0)
            out.append({
                "leg": leg.key, "leg_label": leg.label,
                "symbol": ts_sym, "name": _name_of(ts_sym) or ts_sym,
                "industry": imap.get(ts_sym) or "—",
                "entry_date": entry_date or None,
                "entry_price": pos.get("entry_price"),
                "qty": int(row.get("volume") or 0),
                "last": round(last, 2) if last else None,
                "fpnl": round(float(row.get("fpnl") or 0), 0),
                "fpnl_pct": round(float(row.get("fpnl") or 0)
                                  / max(1e-9, float(row.get("vwap") or 1)
                                        * int(row.get("volume") or 1)) * 100, 1),
                "days_held": days_held, "max_holding": max_hold,
                "expire_date": _expire_date(entry_date, max_hold),
                "stop": stop,
                "dist_stop_pct": round((last / float(stop) - 1) * 100, 1)
                if stop and last else None,
                "tp1_price": pos.get("tp1_price"), "tp2_price": pos.get("tp2_price"),
                "trailing": {"neckline": tr.get("neckline"), "atr": tr.get("atr"),
                             "grace": grace,
                             "in_grace": (days_held or 0) <= grace,
                             "steps_taken": max(0, (days_held or 0) - grace)
                             if tr.get("step") else 0,
                             "step": tr.get("step")},
                "signal": {"neckline": sig.get("neckline"), "rr": sig.get("rr"),
                           "formed_at": sig.get("formed_at"),
                           "entry_theory": sig.get("entry_price"),
                           "atr": sig.get("atr")} if sig else None,
                "kline_summary": kstat,
                "market": {"sh_pct": _market_pct("000001.SH", entry_date),
                           "hs300_pct": _market_pct("000300.SH", entry_date)},
                "_klines": klines,                # prompt 专用，不落盘
                "_kline_entry_idx": k_entry_idx,  # 渲染分界锚，不落盘
                "_leg_dir": str(leg_dir),         # 内部键，落盘前剔除
            })
    return out


def _name_of(ts_sym: str) -> str | None:
    from ops.emquant_eod_report import _name_map
    return _name_map([ts_sym]).get(ts_sym)


# ─────────────────────── LLM 归因（降级不炸） ───────────────────────

def make_prompt(ctx: dict) -> str:
    t = ctx.get("trailing") or {}
    s = ctx.get("signal") or {}
    k = ctx.get("kline_summary") or {}
    m = ctx.get("market") or {}
    kl = "\n".join(",".join(map(str, r)) for r in ctx.get("_klines") or [])
    return _PROMPT_TMPL.format(
        name=ctx.get("name"), symbol=ctx.get("symbol"),
        industry=ctx.get("industry"), leg_label=ctx.get("leg_label"),
        entry_date=ctx.get("entry_date") or "—",
        entry_price=_fmt(ctx.get("entry_price")),
        neckline=_fmt(s.get("neckline")), formed_at=s.get("formed_at") or "—",
        signal_atr=_fmt(s.get("atr")), entry_theory=_fmt(s.get("entry_theory")),
        rr=_fmt(s.get("rr")),
        last=_fmt(ctx.get("last")), fpnl=_fmt(ctx.get("fpnl"), 0),
        fpnl_pct=ctx.get("fpnl_pct"), days_held=ctx.get("days_held"),
        max_holding=ctx.get("max_holding"), expire_date=ctx.get("expire_date") or "—",
        stop=_fmt(ctx.get("stop")), dist_stop_pct=ctx.get("dist_stop_pct"),
        tp1=_fmt(ctx.get("tp1_price")), tp2=_fmt(ctx.get("tp2_price")),
        t_neckline=_fmt(t.get("neckline")), t_atr=_fmt(t.get("atr")),
        grace=t.get("grace"),
        grace_state="grace 保护期内" if t.get("in_grace") else "grace 已过，trailing 收紧中",
        steps_taken=t.get("steps_taken"), step=t.get("step"),
        high=_fmt(k.get("high")), low=_fmt(k.get("low")), dd_pct=k.get("dd_pct"),
        sh_pct=m.get("sh_pct") if m.get("sh_pct") is not None else "—",
        hs300_pct=m.get("hs300_pct") if m.get("hs300_pct") is not None else "—",
        klines=_render_klines(ctx.get("_klines") or [],
                              ctx.get("_kline_entry_idx")))


def _render_klines(klines: list, entry_idx: int | None) -> str:
    """K 线块压缩渲染（09-03 实测：逐根 OHLCV 5 列×25 行会让 glm-5.3 思考
    300s+ 静默超时——任何 budget/effort/流式组合都救不回；压缩后思考量骤降）。

    进场前背景 → 一行收盘序列；进场日起 → 逐日 close/涨跌%/振幅（归因主体，
    ←进场 标记锚在 entry_idx）；量能 → 进场后均量/背景均量倍数（一行）。
    """
    if not klines:
        return "（湖缺该标的日线）"
    if entry_idx is None:                       # 进场日脏数据 → 尾 10 根展开兜底
        entry_idx = max(0, len(klines) - 10)
    head, tail = klines[:entry_idx], klines[entry_idx:]
    lines = []
    if head:
        closes = " ".join(f"{r[4]:g}" for r in head)
        lines.append(f"背景收盘（{head[0][0]}~{head[-1][0]}，{len(head)}日）：{closes}")
        bg_vol = sum(r[5] for r in head) / len(head)
    else:
        bg_vol = None
    prev_c = None
    for r in tail:
        d, _o, h, l, c, v = r
        pct = "" if prev_c is None else f" {(c / prev_c - 1) * 100:+.1f}%"
        amp = f"{(h - l) / l * 100:.1f}%" if l else "—"
        mark = " ←进场" if prev_c is None else ""
        lines.append(f"{d} 收{c:g}{pct} 振幅{amp}{mark}")
        prev_c = c
    if bg_vol and tail:
        after_vol = sum(r[5] for r in tail) / len(tail)
        if bg_vol:
            lines.append(f"量能：进场后均量/背景均量 = {after_vol / bg_vol:.2f}×")
    return "\n".join(lines)


def _parse_llm(text: str) -> dict:
    """剥 ```json 围栏解析结构化块；失败降级整文当 markdown。"""
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group(1))
        except ValueError:
            parsed = None
    if parsed is None:                            # 无围栏：试首 { 到末 } 的整段
        s, e = text.find("{"), text.rfind("}")
        if 0 <= s < e:
            try:
                parsed = json.loads(text[s:e + 1])
            except ValueError:
                parsed = None
    md = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", text, flags=re.S).strip()
    if parsed:
        rs = str(parsed.get("risk_state") or "")
        if rs not in _RISK_STATES:
            parsed["risk_state"] = None           # 词表外 → 前端中性灰
        parsed["markdown"] = md or text.strip()
        return parsed
    return {"primary": None, "secondary": None, "evidence": None,
            "confidence": None, "risk_state": None, "markdown": text.strip()}


def analyze_one(ctx: dict) -> dict:
    """单持仓 LLM 归因。三段韧性：主档双败 → flash 兜底一次 → error 降级。

    实测矩阵（09-03，z.ai Anthropic 端点）：旗舰档（max/high 长思考）在端点
    拥塞时段对归因 prompt 完全静默——max 600s+ 挂起、high 思考吃光 token；
    端点空闲时 max 正常（短 prompt 27s 验证）。故：max 优先（用户指定深度），
    双败降 glm-5.3-flash（同代轻量档）兜底当日产物，analysis 记 actual_model
    供前端标注——深度优先、可用性兜底。思考档 LOSER_REVIEW_EFFORT 可配。
    """
    import time as _time
    from infra.llm import get_llm_client
    from infra.llm.base import LLMConfigError

    effort = os.getenv("LOSER_REVIEW_EFFORT", "max")
    base = get_llm_client()
    fallback = getattr(base, "with_model", lambda _m: base)("glm-5.3-flash")
    # 主档单次（拥塞时 300s 超时即弃，不重试——10 只串行的 cron 窗口预算
    # 有限；空闲时段一次就成，拥塞时段重试也是白等）→ flash 兜底
    attempts = [(base, f"主档 effort={effort}", 0),
                (fallback, "兜底 glm-5.3-flash", 5)]
    last_err: Exception | None = None
    for client, label, delay in attempts:
        if delay:
            _time.sleep(delay)
        try:
            text = client.call(make_prompt(ctx), max_tokens=16384,
                               temperature=0.3,
                               reasoning_effort=effort if client is base else "max")
            parsed = _parse_llm(text)
            if not parsed.get("primary") and not parsed.get("markdown"):
                raise RuntimeError(str(parsed.get("error") or "响应无正文"))
            parsed["actual_model"] = getattr(client, "_model", None)
            return parsed
        except (LLMConfigError, OSError, RuntimeError, ValueError) as e:
            last_err = e
            print(f"    ↻ {ctx['symbol']} {label}失败（{type(e).__name__}），换下一档…")
    return {"primary": None, "secondary": None, "evidence": None,
            "confidence": None, "risk_state": None, "markdown": None,
            "error": f"{type(last_err).__name__}: {last_err}"}


# ─────────────────────── 主链：组装→归因→落盘→台账 ───────────────────────

def run(day: str | None = None, force: bool = False) -> dict:
    from trading.job_ledger import begin_run, finish_run
    day = day or f"{datetime.now():%Y-%m-%d}"
    out = OUT_DIR / f"loser_review_{day}.json"
    if out.exists() and not force:
        print(f"[loser_review] {out.name} 已存在（--force 重跑）")
        return json.loads(out.read_text(encoding="utf-8"))

    begin_run(JOB_NAME, day, datetime.now().isoformat())
    t0 = datetime.now()
    ctxs = build_context()
    print(f"[loser_review] {day} 浮亏持仓 {len(ctxs)} 只，逐只归因…")
    legs_doc: dict[str, dict] = {}
    for ctx in ctxs:
        analysis = analyze_one(ctx)
        state = analysis.get("risk_state") or "未判定"
        print(f"  {ctx['leg']} {ctx['symbol']} {ctx['name']} "
              f"浮亏 {ctx['fpnl_pct']}% → {analysis.get('primary') or analysis.get('error', '—')}"
              f"（{state}）")
        leg_doc = legs_doc.setdefault(ctx["leg"], {"leg": ctx["leg"],
                                                   "label": ctx["leg_label"],
                                                   "rows": []})
        row = {k: v for k, v in ctx.items() if not k.startswith("_")}
        row["analysis"] = analysis
        leg_doc["rows"].append(row)

    doc = {
        "day": day, "generated_at": f"{t0:%Y-%m-%d %H:%M:%S}",
        "model": os.getenv("GLM_MODEL", "glm-4"),
        "legs": list(legs_doc.values()),
        "note": "浮亏持仓（fpnl<0）每日盘后 LLM 深度归因：主/次因+量化证据+风险状态"
                "三档（持有观察/收紧关注/临近风控线）；只读分析，不构成交易指令",
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    finish_run(JOB_NAME, day, "done",
               message=f"{len(ctxs)} 只浮亏归因 @ {doc['model']}")
    print(f"[loser_review] {out.name}（{len(ctxs)} 只，"
          f"{(datetime.now() - t0).total_seconds():.0f}s）")
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="每日亏损持仓 LLM 深度归因")
    p.add_argument("--day", default=None, help="业务日 YYYY-MM-DD（缺省今天）")
    p.add_argument("--force", action="store_true", help="当日产物已存在也重跑")
    args = p.parse_args(argv)
    run(day=args.day, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
