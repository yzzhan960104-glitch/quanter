# -*- coding: utf-8 -*-
"""research/committee/strat_tools.py —— 委员会查询工具层（确定性、只读、中文输出）。

数据源与口径（全部本地，零外网）：
  - diag/neckline_regime_trades.parquet：66,130 笔全量回测语料。口径=B3 champion
    参数、2021 冻结符号集（1202 只）、2020 起暖机、replay 2021-01-01→2026-09-04。
    avg_pnl_pct=毛价收益率%（组合资金权重另论）；holding_bars=持有交易日数。
  - data_lake/index_daily.parquet：指数日线（399006.SZ 创业板指=池正确基准）。
    MA60 自 2021-04、MA200 自 2021-11 起有效（数据始于 2021-01-04）。
  - 7002 网关 + 各腿 state.json：实盘持仓事实（复用 ops.gm_ops_common）。
  - 知识库：docs/research 已定稿结论的固化摘要（防委员会重提已否决方向）。

三区口径（2026-09-05 market-throttle 设计，与 regime 实证一致）：
  above=收盘≥1.00×MA60；wire(缠线带)=0.97~1.00；deep=<0.97。
"""
from __future__ import annotations

import bisect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.committee.llm_tools import ToolCallError  # noqa: E402
from research.committee import kb as _kb               # noqa: E402 —— 知识库三源加载

_TRADES = None          # 惰性缓存（语料/指数各读一次 parquet）
_IDX = None
_CAL: list[str] | None = None
_POSITIONS_TXT: str | None = None


def _trades():
    global _TRADES
    if _TRADES is None:
        import pandas as pd
        _TRADES = pd.read_parquet(ROOT / "diag" / "neckline_regime_trades.parquet")
        for c in ("entry_date", "exit_date", "formed_at"):
            _TRADES[c] = _TRADES[c].astype(str)
        _TRADES["year"] = _TRADES["entry_date"].str[:4].astype(int)
    return _TRADES


def _idx():
    global _IDX
    if _IDX is None:
        import pandas as pd
        df = pd.read_parquet(ROOT / "data_lake" / "index_daily.parquet")
        cyb = df.xs("399006.SZ", level="symbol").sort_index()
        cyb.index = cyb.index.astype(str)
        cyb["ma60"] = cyb["close"].rolling(60).mean()
        cyb["ma200"] = cyb["close"].rolling(200).mean()
        _IDX = cyb
    return _IDX


def _zone(ratio: float | None) -> str:
    if ratio is None or ratio != ratio:            # NaN 防御
        return "unknown"
    if ratio >= 1.0:
        return "above"
    if ratio >= 0.97:
        return "wire"
    return "deep"


def _cal() -> list[str]:
    global _CAL
    if _CAL is None:
        _CAL = list(_idx().index)                  # 创业板指日历=交易日历代理
    return _CAL


# ─────────────────────── 1. 回测语料切片 ───────────────────────

_H_BINS = [(0, 1, "0-1日"), (2, 3, "2-3日"), (4, 7, "4-7日"),
           (8, 15, "8-15日"), (16, 30, "16-30日"), (31, 999, "31日+")]
_R_BINS = [(-99, 1.0, "rr<1"), (1.0, 1.5, "1-1.5"), (1.5, 2.0, "1.5-2"),
           (2.0, 3.0, "2-3"), (3.0, 99, "3+")]
_P_BINS = [(-99, -10, "<=-10%"), (-10, -5, "-10~-5%"), (-5, 0, "-5~0%"),
           (0, 5, "0~5%"), (5, 10, "5~10%"), (10, 99, ">10%")]


def _agg(df) -> str:
    if len(df) == 0:
        return "n=0"
    p = df["avg_pnl_pct"]
    return (f"n={len(df)} 均笔{p.mean():+.2f}% 中位{p.median():+.2f}% "
            f"胜率{(p > 0).mean() * 100:.1f}% 合计毛收益{p.sum():+.0f}pp")


def tool_query_trades(group_by: str = "year", year_min: int = 2021,
                      year_max: int = 2026, exit_reason: str = "",
                      holding_min: int = 0, holding_max: int = 999,
                      rr_min: float = -99, rr_max: float = 99,
                      pnl_min: float = -99, pnl_max: float = 99,
                      sample_n: int = 0) -> str:
    """回测语料（66,130 笔，B3 参数 2021-2026）分组统计/抽样。

    group_by: year|exit_reason|holding_bin|rr_bin|pnl_bin|month|none；
    exit_reason ∈ tp2|stop_loss|timeout；rr=已实现R倍数（盈亏÷风险，**事后
    变量**——不可当事前过滤特征引用）；sample_n>0 时附抽样明细。
    """
    df = _trades()
    m = ((df.year >= year_min) & (df.year <= year_max)
         & (df.holding_bars >= holding_min) & (df.holding_bars <= holding_max)
         & (df.rr >= rr_min) & (df.rr <= rr_max)
         & (df.avg_pnl_pct >= pnl_min) & (df.avg_pnl_pct <= pnl_max))
    if exit_reason:
        m &= df.exit_reason == exit_reason
    df = df[m]
    head = f"筛选后 {_agg(df)}（口径：B3 champion，2021 冻结池 1202 只，毛价收益率）"
    if group_by == "none":
        return head
    if group_by == "year":
        rows = [f"  {y}年: {_agg(g)}" for y, g in df.groupby("year")]
    elif group_by == "month":
        rows = [f"  {m_}月(全年份合并): {_agg(g)}"
                for m_, g in df.groupby(df.entry_date.str[5:7])]
    elif group_by == "exit_reason":
        rows = [f"  {r}: {_agg(g)}" for r, g in df.groupby("exit_reason")]
    elif group_by == "holding_bin":
        rows = [f"  {lbl}: {_agg(df[(df.holding_bars >= lo) & (df.holding_bars <= hi)])}"
                for lo, hi, lbl in _H_BINS]
    elif group_by == "rr_bin":
        rows = [f"  信号rr {lbl}: {_agg(df[(df.rr >= lo) & (df.rr < hi)])}"
                for lo, hi, lbl in _R_BINS]
    elif group_by == "pnl_bin":
        rows = [f"  笔收益 {lbl}: {_agg(df[(df.avg_pnl_pct >= lo) & (df.avg_pnl_pct < hi)])}"
                for lo, hi, lbl in _P_BINS]
    else:
        raise ToolCallError(f"group_by 需 ∈ year|month|exit_reason|holding_bin|"
                            f"rr_bin|pnl_bin|none，收到 {group_by!r}")
    out = [head] + rows
    if sample_n > 0:
        cols = ["symbol", "entry_date", "exit_date", "exit_reason", "rr",
                "holding_bars", "avg_pnl_pct"]
        out.append("抽样：\n" + df[cols].head(sample_n).to_string(index=False))
    return "\n".join(out)


# ─────────────────────── 2. Regime 三区 ───────────────────────

def tool_query_regime(start: str = "", end: str = "", current_only: bool = False,
                      join_trades: bool = True) -> str:
    """创业板指 vs MA60 三区（above/wire/deep）状态与各区开仓表现。

    current_only=true 只报最新状态；join_trades=true 附各区开仓的逐笔统计
    （按 entry_date 当日所在区归属）。口径见模块 docstring。
    """
    idx = _idx()
    last_day = idx.index[-1]
    ratio_now = (idx["close"] / idx["ma60"]).iloc[-1]
    cur = (f"最新交易日 {last_day}：创业板指收盘 {idx['close'].iloc[-1]:.1f}，"
           f"MA60 {idx['ma60'].iloc[-1]:.1f}，比值 {ratio_now:.3f}，"
           f"区={_zone(ratio_now)}；MA200 比值 "
           f"{(idx['close'] / idx['ma200']).iloc[-1]:.3f}")
    if current_only:
        return cur
    m = (idx.index >= (start or "2021-01-01")) & (idx.index <= (end or last_day))
    win = idx[m]
    zc = win["close"].div(win["ma60"]).map(_zone).value_counts()
    out = [cur, f"窗口 {win.index[0]}~{win.index[-1]} 各区交易日数："
           + "，".join(f"{k}={v}" for k, v in zc.items())]
    if join_trades:
        day_zone = (win["close"] / win["ma60"]).map(_zone).to_dict()
        tr = _trades()
        tr_m = tr[tr.entry_date.isin(day_zone)]
        for z in ("above", "wire", "deep"):
            g = tr_m[tr_m.entry_date.map(lambda d: day_zone[d]) == z]
            out.append(f"  区 {z} 开仓: {_agg(g)}（止损占比 "
                       f"{(g.exit_reason == 'stop_loss').mean() * 100:.1f}%）"
                       if len(g) else f"  区 {z} 开仓: n=0")
    return "\n".join(out)


# ─────────────────────── 3. 实盘持仓 ───────────────────────

def tool_query_positions() -> str:
    """实盘双腿持仓事实（7002 网关 ∩ state.json）：成本/现价/浮亏/持有天数/
    止损距离/TP/trailing 进度。数据为调用时点快照。"""
    global _POSITIONS_TXT
    if _POSITIONS_TXT is not None:
        return _POSITIONS_TXT
    from datetime import datetime
    import pandas as pd
    from ops import gm_ops_common as gc
    try:
        sb = pd.read_parquet(ROOT / "data_lake" / "stock_basic.parquet")
        name_map = dict(zip(sb["ts_code"], sb["name"]))
        ind_map = dict(zip(sb["ts_code"], sb["industry"]))
    except Exception:                              # noqa: BLE001 —— 名称缺失不致命
        name_map, ind_map = {}, {}
    cal = _cal()
    today = f"{datetime.now():%Y-%m-%d}"
    token = str(gc.runtime_config().get("token") or "")
    lines = []
    for leg in gc.active_legs():
        leg_dir = gc.leg_strategy_dir(leg)
        cfg = gc.runtime_config(leg_dir)
        lt = str(cfg.get("token") or token)
        la = str(cfg.get("account_id") or "")
        st, payload = gc.api_get(f"/v3/account-trade/positions/{la}", lt, timeout=4.0)
        live = {r.get("symbol"): r for r in ((payload or {}).get("data") or [])
                if isinstance(r, dict) and int(r.get("volume") or 0) > 0} \
            if st == 200 else {}
        try:
            state = json.loads(gc.state_pkl_path(leg_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        sp = state.get("positions") or {}
        lines.append(f"== 腿 {leg.key}（{leg.label}）http={st} 持仓 {len(live)} 只 ==")
        for gm_sym, row in live.items():
            # gm 口径 SZSE.300602 → ts 口径 300602.SZ
            parts = str(gm_sym).split(".")
            ts_sym = f"{parts[1]}.{ 'SH' if parts[0] == 'SHSE' else 'SZ'}"
            pos = next((v for k, v in sp.items() if str(k).split(".")[0] == parts[1]),
                       {})
            entry_date = str(pos.get("entry_date") or "")
            # 持有交易日数=(entry, today]；entry 不在日历（湖滞后/停牌）时
            # bisect 仍给出正确位置——09-04 进场≈0 个完整交易日已过
            days = (bisect.bisect(cal, today) - bisect.bisect(cal, entry_date)) \
                if entry_date else None
            # 现价口径（09-06 实证）：price=实时；last_price/vwap 为陈旧快照字段
            # （300433 实证 last_price=38.91 冻结在 08-28，湖证 09-04 收盘 35.14）
            last = float(row.get("price") or row.get("last_price") or 0)
            stop = pos.get("stop")
            tr = pos.get("trailing") or {}
            fpnl = float(row.get("fpnl") or 0)
            entry_px = pos.get("entry_price")   # 限价帽口径，仅作兜底
            seg = [f"  {ts_sym} {name_map.get(ts_sym, '')} [{ind_map.get(ts_sym, '—')}]"
                   f" 数量{row.get('volume')} 现价{last:.2f} 浮盈亏{fpnl:+.0f}元"]
            # 浮亏% = fpnl / cost（cost=持仓成本金额，最稳口径；09-06 实证 vwap
            # 与 last_price 同源冻结、state entry_price=限价帽——两个坑都实测踩过）
            cost_amt = float(row.get("cost") or 0)
            if cost_amt <= 0:
                cost_amt = (float(row.get("vwap") or 0)
                            * int(row.get("volume") or 1)) or entry_px or 0
            if cost_amt:
                seg.append("（%+.1f%%）" % (fpnl / max(1e-9, cost_amt) * 100))
            seg.append(f" 进场{entry_date or '?'} 已持{days}日"
                       f"/上限{int((pos.get('exec_params') or {}).get('max_holding') or 30)}")
            if stop and last:
                seg.append(f" 止损{stop}（距{(last / float(stop) - 1) * 100:+.1f}%）")
            else:
                seg.append(f" 止损{stop or '?'}")
            seg.append(f" TP1={pos.get('tp1_price')} TP2={pos.get('tp2_price')}"
                       f" 颈线{tr.get('neckline')} grace{tr.get('grace')}步"
                       f"{tr.get('steps_taken')}")
            lines.append("".join(seg))
    _POSITIONS_TXT = "\n".join(lines)
    return _POSITIONS_TXT


# ─────────────────────── 4. 当前配置 ───────────────────────

def tool_query_config() -> str:
    """研究线 champion 全参数 + 语义 + 部署差异说明。"""
    from experiment.resolver import resolve_champion
    from strategies.neckline.schema import NecklineConfig
    c = resolve_champion()
    desc = {n: f.description for n, f in NecklineConfig.model_fields.items()}
    lines = [f"研究线 ACTIVE champion：{c.strategy_name}（{c.version}）",
             "线上掘金底座=R6-8，与研究 champion 语义差 5 键（tp_adapt 开关/"
             "min_supp/max_h_atr/decay_tau 等）——本工具只报研究线口径："]
    for k, v in c.params.items():
        lines.append(f"  {k} = {v}  # {(desc.get(k) or '')[:44]}")
    lines.append("语料口径：上述参数 × 2021 冻结池 1202 只 × 2020 起暖机 × "
                 "replay 2021-01-01→2026-09-04 = 66,130 笔。")
    return "\n".join(lines)


# ─────────────────────── 5. 否定性知识库（三源，见 committee/kb.py） ───────────────────────

def tool_query_knowledge(topic: str = "") -> str:
    """已定稿研究结论/否决知识库（策展 md + REJECTED 提案自动挖掘）。空=目录。"""
    kb = _kb.load_kb()
    if not topic:
        return "可用主题：\n" + "\n".join(f"  {k}" for k in kb)
    if topic not in kb:
        raise ToolCallError(f"未知主题 {topic!r}，可用：{sorted(kb)}")
    return kb[topic]


# ─────────────────────── 6. 指数行情 ───────────────────────

def tool_query_index(ts_code: str = "399006.SZ", days: int = 30) -> str:
    """指数近 N 日行情与均线状态（默认创业板指）。可用：399006.SZ/000300.SH/
    000905.SH/000852.SH/000688.SH。"""
    import pandas as pd
    df = pd.read_parquet(ROOT / "data_lake" / "index_daily.parquet")
    try:
        s = df.xs(ts_code, level="symbol").sort_index()
    except KeyError:
        raise ToolCallError(f"无指数 {ts_code!r}，可用：399006.SZ/000300.SH/"
                            f"000905.SH/000852.SH/000688.SH") from None
    s.index = s.index.astype(str)
    s["ma60"] = s["close"].rolling(60).mean()
    s["ma200"] = s["close"].rolling(200).mean()
    t = s.tail(days)
    rows = [f"  {d} close={r.close:.1f} chg={r.close / s.close.shift(1).loc[d] * 100 - 100:+.2f}%"
            f" vsMA60={r.close / r.ma60:.3f}({'' if r.ma60 != r.ma60 else _zone(r.close / r.ma60)})"
            f" vsMA200={r.close / r.ma200:.3f}"
            for d, r in t.iterrows()]
    return (f"{ts_code} 近 {len(t)} 日（{t.index[0]}~{t.index[-1]}）：\n"
            + "\n".join(rows[-min(days, 40):]))


# ─────────────────────── 注册表 ───────────────────────




# ───────────────────── 基本面工具（2026-09-07 用户指令:分析师补基本面席位）─────────────────────
# 三表同走 (date, symbol) 索引、ts_code 列；全部只读湖内快照。
# 设计红线（kb/pertrade_calibers 已实证:估值/换手等横截面因子对本策略零增量）：
# 基本面=风险上下文与状态描述,不是过滤信号——工具描述里明示,防 agent 重提已否决方向。

def _fund_symbols(symbols: str) -> list:
    syms = [x.strip() for x in symbols.split(",") if x.strip()]
    if syms:
        return syms
    from ops.gm_ops_common import leg_strategy_dir, runtime_config, api_get, active_legs
    out = []
    for leg in active_legs():
        try:
            cfg = runtime_config(leg_strategy_dir(leg))
            st, payload = api_get(f"/v3/account-trade/positions/{cfg.get('account_id')}",
                                  str(cfg.get("token") or ""), timeout=6)
            for r in (payload or {}).get("data") or []:
                ex, code = str(r.get("symbol") or ".").split(".")
                out.append(code + (".SH" if ex == "SHSE" else ".SZ"))
        except Exception:
            pass
    return sorted(set(out))


def tool_query_valuation(symbols: str = "") -> str:
    """估值快照（PE/PB/市值/换手 + 当日全池横截面分位）。symbols=逗号分隔
    ts 代码（空=取实盘双腿全部持仓）。
    ⚠️ 用途=风险上下文：估值因子对信号质量零增量（六年实证），勿据此提过滤提案。"""
    import pandas as pd
    syms = _fund_symbols(symbols)
    if not syms:
        return "（无标的可查——请显式传 symbols）"
    db = pd.read_parquet(ROOT / "data_lake" / "daily_basic.parquet")
    last_day = db.index.get_level_values("date").max()
    day = db.xs(last_day, level="date", drop_level=True)
    pool_pe = day["pe_ttm"].where(day["pe_ttm"] > 0)
    pool_pb = day["pb"].where(day["pb"] > 0)
    lines = [f"全池 {len(day)} 只 @ {last_day.date()}（pe_ttm 中位 {pool_pe.median():.1f} / pb 中位 {pool_pb.median():.2f}）"]
    for ts in syms:
        try:
            r = day.xs(ts)
            pe, pb = float(r.get("pe_ttm") or 0), float(r.get("pb") or 0)
            pe_rank = pool_pe.rank(pct=True).get(ts) if pe > 0 else None
            pb_rank = pool_pb.rank(pct=True).get(ts) if pb > 0 else None
            lines.append(
                f"{ts}: PE(TTM) {pe:.1f}" + (f"（池内 {pe_rank*100:.0f}% 分位）" if pe_rank else "（亏损）")
                + f" | PB {pb:.2f}" + (f"（{pb_rank*100:.0f}% 分位）" if pb_rank else "")
                + f" | 总市值 {float(r.get('total_mv') or 0)/1e4:.0f}亿 | 换手 {float(r.get('turnover_rate') or 0):.2f}%")
        except KeyError:
            lines.append(f"{ts}: 当日无快照")
    return chr(10).join(lines)


def tool_query_earnings(symbols: str = "", days: int = 180) -> str:
    """业绩预告/快报事件（近 days 天）：预告类型（预增/预减/扭亏/首亏…）与
    净利变动区间、快报营收/净利。symbols 同 query_valuation（空=双腿持仓）。"""
    import pandas as pd
    syms = _fund_symbols(symbols)
    if not syms:
        return "（无标的可查——请显式传 symbols）"
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    out = []
    for tbl, name in (("forecast", "业绩预告"), ("express", "业绩快报")):
        df = pd.read_parquet(ROOT / "data_lake" / f"{tbl}.parquet")
        df = df[df.index.get_level_values("symbol").isin(syms)]
        df = df[df.index.get_level_values("date") >= cutoff]
        for (d, ts), r in df.iterrows():
            if name == "业绩预告":
                out.append(f"{d.date()} {ts} 预告[{r.get('type')}] 报告期 {r.get('end_date')}"
                           f" 净利变动 {r.get('p_change_min')}%~{r.get('p_change_max')}%")
            else:
                out.append(f"{d.date()} {ts} 快报 报告期 {r.get('end_date')}"
                           f" 营收 {float(r.get('revenue') or 0)/1e8:.2f}亿 净利 {float(r.get('n_income') or 0)/1e8:.3f}亿")
    return chr(10).join(out) or f"（近 {days} 天无业绩事件）"


def tool_query_financials(symbols: str = "") -> str:
    """利润表近三期：营收/归母净利及同比。symbols 同上（空=双腿持仓）。"""
    import pandas as pd
    syms = _fund_symbols(symbols)
    if not syms:
        return "（无标的可查——请显式传 symbols）"
    df = pd.read_parquet(ROOT / "data_lake" / "fina_income.parquet")
    out = []
    for ts in syms:
        sub = df[df.index.get_level_values("symbol") == ts].sort_index()
        if sub.empty:
            out.append(f"{ts}: 无财务数据")
            continue
        prev = None
        for ((d, _), r) in sub.iloc[-3:].iterrows():
            rev, ni = float(r.get("total_revenue") or 0), float(r.get("n_income") or 0)
            yoy = f" 营收同比{(rev/prev[0]-1)*100:+.1f}%" if prev and prev[0] else ""
            yoy_n = f" 净利同比{(ni/prev[1]-1)*100:+.1f}%" if prev and prev[1] else ""
            out.append(f"{ts} 报告期 {r.get('end_date')}: 营收 {rev/1e8:.2f}亿 归母净利 {ni/1e8:.3f}亿{yoy}{yoy_n}")
            prev = (rev, ni)
    return chr(10).join(out)


def build_tools() -> dict[str, tuple[dict, object]]:
    """anthropic tools 注册表：name -> (schema, 实现函数)。"""
    specs = [
        ("query_trades", tool_query_trades,
         "回测语料统计：66,130 笔（B3 参数 2021-2026 全期）。支持按年份/月份/"
         "出场原因/持有期/信号rr/笔收益分组与多维过滤，可抽样明细。"),
        ("query_regime", tool_query_regime,
         "创业板指 vs MA60 三区（above/wire缠线带/deep）状态、各区交易日分布、"
         "各区开仓的逐笔表现。策略期望已实证全部来自 above 区。"),
        ("query_positions", tool_query_positions,
         "实盘双腿当前持仓事实：现价/浮亏/持有天数/止损距离/TP/trailing 进度。"),
        ("query_config", tool_query_config,
         "研究线 ACTIVE champion 全部 21+ 维参数与语义描述。"),
        ("query_knowledge", tool_query_knowledge,
         "已定稿研究结论库（七波过滤否决/regime 实证/节流设计/R10 终审/幸存者"
         "偏差红线/口径地图/time_stop 史）——提案前必查，防止重提已否决方向。"),
        ("query_index", tool_query_index,
         "指数近 N 日行情与 MA60/MA200 状态（默认创业板指 399006.SZ）。"),
        ("query_valuation", tool_query_valuation,
         "估值快照：PE/PB/市值/换手 + 当日全池横截面分位（symbols 缺省=实盘持仓）。"
         "⚠️ 风险上下文专用——估值因子对信号质量零增量（六年实证），勿据此提过滤提案。"),
        ("query_earnings", tool_query_earnings,
         "业绩预告/快报事件（预增预减扭亏/营收净利区间），按标的查询近 180 天。"),
        ("query_financials", tool_query_financials,
         "利润表近三期营收/归母净利及同比（基本面趋势核对）。"),
    ]
    import inspect
    out: dict[str, tuple[dict, object]] = {}
    for name, fn, desc in specs:
        sig = inspect.signature(fn)
        props, required = {}, []
        for pname, p in sig.parameters.items():
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
            typ = {"str": "string", "bool": "boolean", "int": "integer",
                   "float": "number"}.get(getattr(ann, "__name__", str(ann)), "string")
            props[pname] = {"type": typ}
            if p.default is inspect.Parameter.empty:
                required.append(pname)
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        out[name] = ({"name": name, "description": desc,
                      "input_schema": schema}, fn)
    return out
