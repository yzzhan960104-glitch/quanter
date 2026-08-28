# -*- coding: utf-8 -*-
"""R9 · 广度探索循环 · 参数面之外的七个正交维度（2026-08-28，方案见
docs/superpowers/plans/2026-08-28-r9-breadth-exploration.md）。

════════════════════════════════════════════════════════════════════
预登记协议（先于发射写死——此刻未看过任何新维度读数）
════════════════════════════════════════════════════════════════════
正当性：R8 收官（VETO/150 evals）宣告识别 12 维+执行 17 维双口径穷尽。
本轮按用户指令转最广度：扫从未成为搜索维度的七个正交面——
  D1 流动性门槛（universe 30 日均额下限，现 1e5 千元从未被扫）
  D2 周内择时（buy_date 星期过滤：禁周一/禁周五/只二三四）
  D3 个股动量闸窗口族（momentum_gate 只有 20 日一档，窗口从未扫）
  D4 入场价格带（entry 价：≥5 / ≥10 / ≤100 元）
  D5 突破量比上限（breakout_vol_mult 只有下限，巨量=情绪顶假设未验）
  D6 形态深度双边带（max_h_atr 只有上限 5.5）
  D7 次新过滤（全湖真实首日距信号 ≥400 自然日 ≈250 交易日）

目标：inner 2021-24 冻结 PM（4×7.5% 整手+min5+freeze@100w）×
queue_order=random × 种子的配对差值中位（CRN）。outer 2025-26 只进终审。

预登记闸（两段式，R8 同款 + 过滤类换手放宽）：
  Phase A 弱化晋升（K=3）：Δ中位 ≥ +0.015 且 3/3 同向；
  Phase B 采纳（K=5）：G1 Δ中位≥+0.02 / G2 ≥4/5 同向 / G3 逐年恶化≤10pp /
    G4 换手 n_taken≥基线 70%（D1 类）或 ≥50%（D2-D7 过滤类——预登记放宽，
    过滤天然砍信号）/ G5 oracle 全期 Δ≥−0.05；
  Phase C 终审（K=21 配对 vs incumbent0）：outer holdout 中位 Δ≥0 且
    ≥15/21 同向，否则 fail-closed 维持 incumbent。

排程（硬预算 --hours 8）：Boot（湖+首日表+incumbent 锚）→ Phase A 广度
19 探针（≤1.5h；D2-D7 后过滤近免费，D1 四档吃识别缓存增量）→ Phase B
贪心（状态={liquidity 档, 过滤集合}，连续 2 轮零采纳提前收敛，≤4.5h）→
Phase C 终审 K=21（≤1h）。

红线：C2 内核零触碰（D1-D7 全在 loop 层）；ADR-16 池子侧不进参数（D2/D3/D7
是个股信号质量侧，R4-H1 先例边界）；pos_cap/max_positions 不动；闸中途不改；
DRAFT 物化留手动。交付=读数地图（无论采纳与否，地图即战役级交付）。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/r9_breadth_loop.py --hours 8
    ... --smoke   # 抽样 60 只端到端管道验证
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

OUT_DIR = "logs/r9_breadth_loop"
STATE_PATH = os.path.join(OUT_DIR, "state.json")
LAKE_PATH = "data_lake/a_shares_daily.parquet"
LAKE_START = "2021-01-01"
BASE_LIQUIDITY = 1e5   # 千元（30 日均额）；与 discovery.snapshot.load_universe 同值

LOOP_SEEDS = [0, 1, 2, 3, 4]
PHASEA_SEEDS = [0, 1, 2]
FINAL_SEEDS = list(range(21))

G1_TH = 0.02
G2_MIN = 4
G3_MAXDROP = 0.10
G4_MIN_TAKEN_UNIVERSE = 0.70   # D1 类
G4_MIN_TAKEN_FILTER = 0.50     # D2-D7 过滤类（预登记放宽：过滤天然砍信号）
G5_ORACLE_FLOOR = -0.05
A_TH, A_SIGN = 0.015, 3
FINAL_SIGN_MIN = 15

# —— 七维预登记档位（发射前写死，循环中不改）——
DIMS = {
    "D1_liq": {"kind": "universe", "levels": [3e4, 5e4, 2e5, 5e5]},
    "D2_dow": {"kind": "filter", "levels": ["no_mon", "no_fri", "mid_only"]},
    "D3_mom": {"kind": "filter", "levels": [("mom10_ge0", 10, 0.0),
                                             ("mom40_ge0", 40, 0.0),
                                             ("mom20_ge5pct", 20, 0.05),
                                             ("mom20_ge_neg5pct", 20, -0.05)]},
    "D4_price": {"kind": "filter", "levels": ["p_ge5", "p_ge10", "p_le100"]},
    "D5_volcap": {"kind": "filter", "levels": [3.0, 5.0]},
    "D6_band": {"kind": "filter", "levels": [(1.0, 3.5), (1.5, 4.5)]},
    "D7_listed": {"kind": "filter", "levels": [400]},
}
FILTER_DIMS = {k: v for k, v in DIMS.items() if v["kind"] == "filter"}


def _pm(seed=None, **kw):
    from backtest.models import PositionModel
    d = dict(capital=1_000_000, lot_size=100, min_fee=5.0,
             max_positions=4, pos_cap=0.075, freeze_pending=True)
    if seed is not None:
        d.update(queue_order="random", queue_seed=seed)
    d.update(kw)
    return PositionModel(**d)


def _seg(name, start, end):
    from discovery.split import Segment
    return Segment(name, start, end)


def _filled_dicts(filled):
    """R8 同款 + R9 扩展：保留 H_over_ATR/breakout_vol_ratio 供 D5/D6 过滤
    （portfolio_metrics 只读 signal_date/avg_pnl_pct，多带键无害）。"""
    return [{"symbol": r["symbol"], "signal_date": r["signal_date"],
             "buy_date": r["buy_date"], "exit_date": r["exit_date"],
             "avg_pnl_pct": r["avg_pnl_pct"], "entry": r.get("entry"),
             "H_over_ATR": r.get("H_over_ATR"),
             "breakout_vol_ratio": r.get("breakout_vol_ratio")}
            for r in filled]


def _ann(filled_dicts, seg, udates, pm):
    from discovery.objective import portfolio_metrics
    return portfolio_metrics(filled_dicts, seg, udates, position_model=pm)


def build_universe(lake, thresh):
    """按流动性阈值构造 universe——与 discovery.snapshot.load_universe 逐行
    同式（tail(30) 均额 / is_target_board 创科 / xs sort_index），仅阈值参数化。
    自建而非改 snapshot.py：内核零触碰红线 + diag 自包含惯例。"""
    from discovery.snapshot import is_target_board
    amt = lake.groupby("symbol")["amount"].apply(
        lambda s: s.tail(30).mean() if len(s) > 0 else 0.0)
    syms = lake.index.get_level_values("symbol").unique().tolist()
    tradable = [s for s in syms if is_target_board(s) and amt.get(s, 0.0) >= thresh]
    universe = {}
    for s in tradable:
        try:
            universe[s] = lake.xs(s, level="symbol").sort_index()
        except Exception:
            continue
    return universe


class Runner:
    """状态评估器：state = {"liq": 档, "filters": {dim: level, ...}}。
    filled 按 liquidity 档缓存（识别缓存跨档复用：同 id_cfg 同 sym_df →
    _cached_identify_events 命中，D1 换档只有增量 sym 需要识别）。"""

    def __init__(self, base_params, lake, udates, split, first_dates):
        self.base_params = base_params
        self.lake = lake
        self.udates = udates
        self.inner, self.outer = split.inner, split.outer
        self.full = _seg("full", date(2021, 1, 1), date(2026, 12, 31))
        self.first_dates = first_dates
        self._filled_cache = {}
        self._sym_close = {}
        self._mom_cache = {}
        self.n_eval = 0

    # —— 湖访问（D1 变体/D3 动量都从驻留 lake 现取，不依赖 universe 对象）——
    def sym_close(self, sym):
        if sym not in self._sym_close:
            self._sym_close[sym] = self.lake.xs(sym, level="symbol").sort_index()["close"]
        return self._sym_close[sym]

    def mom_value(self, sym, ts, n):
        key = (sym, n)
        if key not in self._mom_cache:
            close = self.sym_close(sym)
            self._mom_cache[key] = close / close.shift(n) - 1.0
        s = self._mom_cache[key]
        if ts in s.index:
            v = s.loc[ts]
            return None if pd.isna(v) else float(v)
        return None

    # —— 过滤器族（D2-D7；None/缺数据中性放行=数据不足不误杀，R4-H1 同语义）——
    # ⚠️ W5-4（2026-08-28 全库评审 P1-5）D2_dow 是 KNOWN_NON_DEPLOYABLE 维度：
    # buy_date（成交日）是市场决定的未来随机变量——挂单何时触发不受交易者控制，
    # 「禁周一成交」无法在信号日执行。该维即使过预登记闸也不可采纳（回测增益
    # 实盘不可达的假 edge），保留仅作统计挖掘对照；采纳闸对 D2_* 应恒 VETO。
    def keep(self, rec, dim, lv):
        if dim == "D2_dow":
            dow = pd.Timestamp(rec["buy_date"]).dayofweek
            return dow != 0 if lv == "no_mon" else dow != 4 if lv == "no_fri" \
                else dow in (1, 2, 3)
        if dim == "D3_mom":
            _tag, n, th = lv
            v = self.mom_value(rec["symbol"], pd.Timestamp(rec["signal_date"]), n)
            return True if v is None else v >= th
        if dim == "D4_price":
            e = rec.get("entry")
            if e is None:
                return True
            return e >= 5 if lv == "p_ge5" else e >= 10 if lv == "p_ge10" else e <= 100
        if dim == "D5_volcap":
            vr = rec.get("breakout_vol_ratio")
            return True if vr is None else float(vr) <= float(lv)
        if dim == "D6_band":
            h = rec.get("H_over_ATR")
            if h is None or pd.isna(h):
                return True
            return float(lv[0]) <= float(h) <= float(lv[1])
        if dim == "D7_listed":
            fd = self.first_dates.get(rec["symbol"])
            if fd is None:
                return True
            return (pd.Timestamp(rec["signal_date"]) - pd.Timestamp(fd)).days >= int(lv)
        raise ValueError(f"unknown dim {dim}")

    def filled_for(self, liq):
        if liq not in self._filled_cache:
            from discovery.objective import run_full_scan
            uni = build_universe(self.lake, liq)
            print(f"[scan] liquidity={liq:.0f} universe={len(uni)} "
                  f"（已缓存档 {sorted(self._filled_cache)}）", flush=True)
            self._filled_cache[liq] = _filled_dicts(run_full_scan(self.base_params, uni))
            del uni
        return self._filled_cache[liq]

    def filled_of(self, state):
        filled = self.filled_for(state["liq"])
        if not state["filters"]:
            return filled
        return [r for r in filled
                if all(self.keep(r, d, lv) for d, lv in state["filters"].items())]

    def eval_state(self, state, seeds):
        """K 种子配对评估：返回 {seed: inner deployable ann} 或 None（异常）。"""
        try:
            filled = self.filled_of(state)
        except Exception:
            print("[eval][WARN] filled 构造异常跳过", flush=True)
            return None
        self.n_eval += 1
        return {s: _ann(filled, self.inner, self.udates, _pm(seed=s))["ann"]
                for s in seeds}

    def full_gates(self, state, base_cache, seeds):
        """采纳前全闸 G3 逐年 / G4 换手 / G5 oracle。"""
        filled = self.filled_of(state)
        # G4 档位（预登记）：候选状态含任一 filter 维 → 过滤类 50%；纯 D1 → 70%
        g4_min = (G4_MIN_TAKEN_FILTER if _state_kind(state) == "filter"
                  else G4_MIN_TAKEN_UNIVERSE)
        cand_inner = {s: _ann(filled, self.inner, self.udates, _pm(seed=s))
                      for s in seeds}
        cand_cache = {"inner_anns": {s: m["ann"] for s, m in cand_inner.items()},
                      "n_taken_med": float(np.median(
                          [m["n_taken"] for m in cand_inner.values()])),
                      "yearly": {}, "oracle_full": None}
        ok = True
        for y in (2021, 2022, 2023, 2024):
            seg = _seg(f"y{y}", date(y, 1, 1), date(y, 12, 31))
            med = float(np.median([_ann(filled, seg, self.udates, _pm(seed=s))["ann"]
                                   for s in seeds]))
            cand_cache["yearly"][y] = round(med, 4)
            if base_cache["yearly"][y] - med > G3_MAXDROP:
                ok = False
        if cand_cache["n_taken_med"] < g4_min * base_cache["n_taken_med"]:
            ok = False
            cand_cache["g4_fail"] = True
        o = _ann(filled, self.full, self.udates, _pm())["ann"]
        cand_cache["oracle_full"] = round(o, 4)
        if o - base_cache["oracle_full"] < G5_ORACLE_FLOOR:
            ok = False
        return ok, cand_cache


def _state_kind(state):
    """G4 档位判定：状态含任一 filter 维 → 过滤类；否则（纯 D1 变档）
    → universe 类。"""
    return "filter" if state["filters"] else "universe"


def _save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, STATE_PATH)


def _fmt_lv(lv):
    return str(lv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--smoke", action="store_true",
                    help="抽样 60 只 + 每维 1 档探针，端到端管道验证")
    ap.add_argument("--resume", action="store_true",
                    help="从 state.json 断点续跑（恢复 phaseA_map/adopted，"
                         "跳过 Phase A；boot 识别缓存进程内已丢须重算）")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    hard_end = t0 + args.hours * 3600
    phaseA_end = t0 + 1.5 * 3600
    phaseB_end = t0 + 7.0 * 3600
    if args.smoke:
        hard_end = phaseA_end = phaseB_end = t0 + 3600

    from discovery.split import extended_split

    print(f"[boot] 读湖（{LAKE_START} 起，pyarrow filters）...", flush=True)
    try:
        lake = pd.read_parquet(LAKE_PATH, filters=[("date", ">=", pd.Timestamp(LAKE_START))])
    except Exception:
        print("[boot][WARN] filters 推送失败，全量读再筛", flush=True)
        lake = pd.read_parquet(LAKE_PATH)
        lake = lake[lake.index.get_level_values("date") >= pd.Timestamp(LAKE_START)]

    print("[boot] 全湖首日表（D7 次新过滤用，columns=[] 纯索引）...", flush=True)
    full_idx = pd.read_parquet(LAKE_PATH, columns=[]).index
    first_dates = pd.Series(full_idx.get_level_values("date"),
                            index=full_idx.get_level_values("symbol"),
                            dtype="object").groupby(level=0).min()
    first_dates = {k: pd.Timestamp(v) for k, v in first_dates.items()}
    del full_idx
    print(f"[boot] 首日表 {len(first_dates)} 只 "
          f"({min(first_dates.values()).date()}~{max(first_dates.values()).date()})",
          flush=True)

    if args.smoke:
        rng = np.random.default_rng(42)
        all_syms = pd.Index(lake.index.get_level_values("symbol").unique())
        keep_syms = set(rng.choice(all_syms.to_numpy(), size=60, replace=False))
        lake = lake[lake.index.get_level_values("symbol").isin(keep_syms)]
        print(f"[smoke] 湖裁剪到 {len(keep_syms)} 只", flush=True)

    from discovery.snapshot import snapshot_hash, _count_stale_symbols
    uni = build_universe(lake, BASE_LIQUIDITY)
    dates_all = [d for df in uni.values() for d in df.index]
    date_range = (f"{pd.Timestamp(min(dates_all)).date()}~"
                  f"{pd.Timestamp(max(dates_all)).date()}") if dates_all else "empty"
    n_stale = _count_stale_symbols(uni)
    shash = snapshot_hash(len(uni), date_range, LAKE_START, n_stale=n_stale)
    print(f"[boot] base universe={len(uni)}（liquidity≥{BASE_LIQUIDITY:.0f}） "
          f"range={date_range} hash={shash} stale={n_stale}", flush=True)
    udates = next(iter(uni.values())).index
    split = extended_split()
    del uni

    base = json.load(open("logs/r6_10_loop/state.json", encoding="utf-8"))["base"]
    R = Runner(base, lake, udates, split, first_dates)

    # —— incumbent 基线锚（B3 + liquidity=1e5 + 零过滤）——
    print("[boot] incumbent 扫描（首次全识别，后续缓存）...", flush=True)
    inc_state = {"liq": BASE_LIQUIDITY, "filters": {}}
    inc_filled = R.filled_for(BASE_LIQUIDITY)
    print(f"[boot] incumbent filled={len(inc_filled)} 笔", flush=True)
    inc_inner = {s: _ann(inc_filled, R.inner, R.udates, _pm(seed=s))["ann"]
                 for s in FINAL_SEEDS}
    inc_outer = {s: _ann(inc_filled, R.outer, R.udates, _pm(seed=s))["ann"]
                 for s in FINAL_SEEDS}
    inc_oracle_full = _ann(inc_filled, R.full, R.udates, _pm())["ann"]
    base_cache = {
        "inner_anns": {s: inc_inner[s] for s in LOOP_SEEDS},
        "n_taken_med": float(np.median(
            [_ann(inc_filled, R.inner, R.udates, _pm(seed=s))["n_taken"]
             for s in LOOP_SEEDS])),
        "yearly": {y: round(float(np.median(
            [_ann(inc_filled, _seg(f"y{y}", date(y, 1, 1), date(y, 12, 31)),
                  R.udates, _pm(seed=s))["ann"] for s in LOOP_SEEDS])), 4)
            for y in (2021, 2022, 2023, 2024)},
        "oracle_full": round(inc_oracle_full, 4),
    }
    inc0 = {"params": base, "state": inc_state, "inner": inc_inner,
            "outer": inc_outer, "oracle_full": round(inc_oracle_full, 4)}
    st = {"started_at": datetime.now().isoformat(), "hours": args.hours,
          "smoke": args.smoke, "base": base, "base_state": inc_state,
          "base_cache": base_cache, "incumbent0": inc0, "adopted": [],
          "phaseA_map": [], "history": [], "rounds_no_adopt": 0,
          "n_eval": R.n_eval, "phase": "boot"}
    skip_phaseA = False
    if args.resume and not args.smoke:
        # 断点续跑：崩在 Phase B 记账行的现场（phaseA_map/adopted 已落盘；
        # rounds_no_adopt 取 state 值不加先验——中断轮不臆断采纳状态）
        try:
            old = json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            old = None
        if old and old.get("phase") in ("A", "B") and not old.get("smoke"):
            st["phaseA_map"] = old.get("phaseA_map", [])
            st["adopted"] = old.get("adopted", [])
            st["history"] = old.get("history", [])
            st["rounds_no_adopt"] = old.get("rounds_no_adopt", 0)
            st["resumed_from"] = old.get("started_at")
            skip_phaseA = len(st["phaseA_map"]) > 0
            print(f"[resume] 恢复：phaseA_map {len(st['phaseA_map'])} 条 / "
                  f"adopted {len(st['adopted'])} 步 / rounds_no_adopt "
                  f"{st['rounds_no_adopt']}（源 {old.get('started_at')}）",
                  flush=True)
        else:
            print("[resume][WARN] 旧 state 不可用（smoke 残留或 phase 不符），"
                  "冷启动", flush=True)
    _save_state(st)
    print(f"[boot] 基线锚：inner deployable 中位 "
          f"{np.median(list(base_cache['inner_anns'].values())):+.3f} / "
          f"oracle 全期 {inc_oracle_full:+.3f} / n_taken "
          f"{base_cache['n_taken_med']:.0f}", flush=True)

    def eval_step(dim, lv, state, base_cache, seeds):
        """评估单变体（state 单步改动）→ 配对差值。D1 换档保留既有过滤。"""
        cand_state = {"liq": lv if dim == "D1_liq" else state["liq"],
                      "filters": {**state["filters"]}}
        if dim != "D1_liq":
            cand_state["filters"][dim] = lv
        cand = R.eval_state(cand_state, seeds)
        if cand is None:
            return None, cand_state, {"dim": dim, "lv": _fmt_lv(lv), "err": True}
        deltas = {s: cand[s] - base_cache["inner_anns"][s] for s in seeds}
        d_med = float(np.median(list(deltas.values())))
        n_pos = sum(1 for v in deltas.values() if v > 0)
        info = {"dim": dim, "lv": _fmt_lv(lv), "d_med": round(d_med, 4),
                "n_pos": n_pos, "n_seeds": len(seeds)}
        return cand, cand_state, info

    def try_adopt(dim, lv, state, base_cache, seeds):
        """Phase B 单步采纳尝试：G1/G2 筛选 → G3-G5 全闸。"""
        cand, cand_state, info = eval_step(dim, lv, state, base_cache, seeds)
        if cand is None:
            return False, state, base_cache, info
        th, smin = G1_TH, G2_MIN
        info["screen_pass"] = bool(info["d_med"] >= th and info["n_pos"] >= smin)
        print(f"[eval] {dim}={_fmt_lv(lv)} Δ中位{info['d_med']:+.4f} "
              f"同向{info['n_pos']}/{len(seeds)}"
              f"{' → 过筛' if info['screen_pass'] else ''}", flush=True)
        if not info["screen_pass"]:
            return False, state, base_cache, info
        ok, cand_cache = R.full_gates(cand_state, base_cache, seeds)
        info["gates_pass"] = ok
        info["cand_cache"] = ({k: v for k, v in cand_cache.items()
                               if k != "inner_anns"} if ok else None)
        print(f"[gate] {dim}={_fmt_lv(lv)} G3-5 {'过' if ok else '否'} "
              f"(oracle Δ{cand_cache['oracle_full'] - base_cache['oracle_full']:+.3f} "
              f"taken {cand_cache['n_taken_med']:.0f})", flush=True)
        if not ok:
            return False, state, base_cache, info
        return True, cand_state, cand_cache, info

    # ── Phase A：七维广度扫描（弱化晋升闸 K=3）──
    st["phase"] = "A"
    _save_state(st)
    if skip_phaseA:
        print(f"\n[PhaseA] resume 跳过（{len(st['phaseA_map'])} 条读数已在盘）",
              flush=True)
    else:
        print(f"\n[PhaseA] 七维广度探针开始（K={len(PHASEA_SEEDS)} 配对，弱化闸）",
              flush=True)
        for dim, spec in DIMS.items():
            if time.time() > phaseA_end:
                print("[PhaseA] 时窗到，提前收", flush=True)
                break
            levels = spec["levels"][:1] if args.smoke else spec["levels"]
            for lv in levels:
                if time.time() > phaseA_end:
                    break
                _, _, info = eval_step(dim, lv, inc_state, base_cache, PHASEA_SEEDS)
                if info.get("err"):
                    continue
                info["weak_promote"] = bool(info["d_med"] >= A_TH
                                            and info["n_pos"] >= A_SIGN)
                st["phaseA_map"].append(info)
                st["n_eval"] = R.n_eval
                _save_state(st)
                print(f"[PhaseA] {dim}={_fmt_lv(lv)} Δ中位{info['d_med']:+.4f} "
                      f"同向{info['n_pos']}/{len(PHASEA_SEEDS)}"
                      f"{' ★弱晋升候选' if info['weak_promote'] else ''}", flush=True)
    if args.smoke:
        st["phase"] = "smoke_done"
        st["n_eval"] = R.n_eval
        _save_state(st)
        print(f"\n[smoke done] {len(st['phaseA_map'])} 探针跑通，管道验证 OK "
              f"({(time.time() - t0) / 60:.1f}min)", flush=True)
        return

    # ── Phase B：贪心（K=5 全闸；访问序=Phase A 读数降序）──
    order_keys = [i["dim"] for i in
                  sorted(st["phaseA_map"], key=lambda x: -x["d_med"])
                  if i["d_med"] > 0]
    dim_order = order_keys + [d for d in DIMS if d not in order_keys]
    st["phase"] = "B"
    _save_state(st)
    print(f"\n[PhaseB] 贪心开始（维序={dim_order}）", flush=True)
    state, cache = inc_state, base_cache
    rnd = 0
    while time.time() < phaseB_end:
        rnd += 1
        n_adopt_round = 0
        for dim in dim_order:
            if time.time() > phaseB_end - 150:
                break
            for lv in DIMS[dim]["levels"]:
                if time.time() > phaseB_end - 150:
                    break
                cur = state["liq"] if dim == "D1_liq" else state["filters"].get(dim)
                if cur == lv or (dim == "D1_liq" and lv == BASE_LIQUIDITY):
                    continue
                adopted, state, cache, info = try_adopt(
                    dim, lv, state, cache, LOOP_SEEDS)
                if adopted:
                    n_adopt_round += 1
                    st["adopted"].append({**info,
                                          "at": datetime.now().isoformat(),
                                          "state_after": dict(
                                              liq=state["liq"],
                                              filters=dict(state["filters"]))})
                    st["base_state"], st["base_cache"] = state, cache
                    st["n_eval"] = R.n_eval
                    _save_state(st)
                    print(f"[ADOPT] {dim}={_fmt_lv(lv)} → base 更新（累计 "
                          f"{len(st['adopted'])} 步；state={state}）", flush=True)
                    break   # 每维取首个过闸档，下轮再细
        st["history"].append({"round": rnd, "n_eval": R.n_eval,
                              "n_adopt": n_adopt_round,
                              "at": datetime.now().isoformat()})
        st["rounds_no_adopt"] = 0 if n_adopt_round else st["rounds_no_adopt"] + 1
        st["n_eval"] = R.n_eval
        _save_state(st)
        print(f"[round {rnd}] 采纳 {n_adopt_round} 步 / evals={R.n_eval} / "
              f"连续零采纳 {st['rounds_no_adopt']}", flush=True)
        if st["rounds_no_adopt"] >= 2:
            print("[PhaseB] 连续 2 轮零采纳——自然收敛", flush=True)
            break
        if time.time() > hard_end - 1800:
            print("[PhaseB] 预算护堤（留 30min 终审），提前收", flush=True)
            break

    # ── Phase C：终审（K=21 配对 vs incumbent0）──
    print(f"\n[PhaseC] 终审 K=21：champion vs incumbent0", flush=True)
    champ_filled = R.filled_of(state)
    ch_inner = {s: _ann(champ_filled, R.inner, R.udates, _pm(seed=s))["ann"]
                for s in FINAL_SEEDS}
    ch_outer = {s: _ann(champ_filled, R.outer, R.udates, _pm(seed=s))["ann"]
                for s in FINAL_SEEDS}
    ch_oracle_full = _ann(champ_filled, R.full, R.udates, _pm())["ann"]
    d_in = [ch_inner[s] - inc0["inner"][s] for s in FINAL_SEEDS]
    d_out = [ch_outer[s] - inc0["outer"][s] for s in FINAL_SEEDS]
    outer_med = float(np.median(d_out))
    outer_sign = sum(1 for v in d_out if v > 0)
    verdict = ("PASS" if (len(st["adopted"]) > 0
                          and outer_med >= 0 and outer_sign >= FINAL_SIGN_MIN)
               else "VETO" if len(st["adopted"]) > 0 else "NULL（零采纳）")
    final = {
        "verdict": verdict,
        "n_adopted": len(st["adopted"]),
        "adopted": st["adopted"],
        "champion_state": {"liq": state["liq"],
                           "filters": dict(state["filters"])},
        "champion_params": base,
        "inner": {"median_delta": round(float(np.median(d_in)), 4),
                  "sign": f"{sum(v > 0 for v in d_in)}/21",
                  "champ_med": round(float(np.median(list(ch_inner.values()))), 4),
                  "inc_med": round(float(np.median(list(inc0['inner'].values()))), 4)},
        "outer_holdout": {"median_delta": round(outer_med, 4),
                          "sign": f"{outer_sign}/21",
                          "champ_med": round(float(np.median(list(ch_outer.values()))), 4),
                          "inc_med": round(float(np.median(list(inc0['outer'].values()))), 4)},
        "oracle_full": {"champ": round(ch_oracle_full, 4),
                        "inc": inc0["oracle_full"]},
        "phaseA_map": st["phaseA_map"],
        "n_eval": R.n_eval,
        "took_min": round((time.time() - t0) / 60, 1),
        "completed_at": datetime.now().isoformat(),
    }
    st["final"] = final
    st["phase"] = "C"
    _save_state(st)
    with open(os.path.join(OUT_DIR, "final_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: final[k] for k in
                      ("verdict", "n_adopted", "champion_state", "inner",
                       "outer_holdout", "oracle_full", "took_min")},
                     ensure_ascii=False, indent=1, default=str), flush=True)
    if verdict == "PASS":
        print("[PhaseC] 过闸——DRAFT 物化留手动（champion_state+params 在 "
              "final_report.json；promote 走 experiment CLI）", flush=True)
    elif verdict.startswith("VETO"):
        print("[PhaseC] 外层 holdout 否决——维持 incumbent（fail-closed）", flush=True)
    else:
        print("[PhaseC] 零采纳——七维读数地图收档（地图即交付）", flush=True)
    print(f"[done] {OUT_DIR}/final_report.json "
          f"({(time.time() - t0) / 3600:.2f}h, evals={R.n_eval})", flush=True)


if __name__ == "__main__":
    main()
