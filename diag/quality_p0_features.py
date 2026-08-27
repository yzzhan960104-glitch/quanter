# -*- coding: utf-8 -*-
"""信号质量 P0 · 逐笔特征库构建（2026-08-26 方案 Phase 0）。

一次全历史扫描（B3 ACTIVE 参数 + freeze("2021-01-01") 今日截面池）→ 每笔成交富化
五族特征（几何/动量/量能/位置/环境）→ logs/quality/trades_features.parquet。
另跑 wf 四折诚实池（load_universe_window 折末选股）的 OOS 段特征库 → 同文件 pool 列区分。

设计要点（与方案/任务书对齐）：
  - 锚验证：outer 2026 × 冻结口径 PM（4×7.5% 整手+min5+freeze_pending）必须复现
    r6_11c 的 ann≈+399.8% / taken=442 / win≈75.8%，不吻合即中止——特征库不允许建在
    与对照线不同口径的流水上；
  - exec 约定：只含成交笔（skip_no_pullback/skip_target_met 不入特征库，任务书口径）；
  - 事件元组：scan_symbol 后同迭代取 _cached_identify_events（缓存必命中），拿到
    (neckline, bottom, atr, formed_at)——filled 记录里没有 bottom/atr；
  - 触及数/底部离散度/压制度：在窗口切片上按内核同口径复刻（聚集带 |top−c*|≤ATR、
    底部带 [min, min+ATR]、衰减加权 close<c* 比例）——C2 冻结只锁修改，不锁调用；
  - 可用性标签（P2 分层红线）：分层分只在 signal_date 收盘可知的特征上构造
    （几何/动量/量能/环境 + same_day_n）；wait_days/entry_depth/is_chase 是成交后
    才可知，只供 P1 归因（meta 里登记 avail 分类，报告复述）；
  - 环境族（pool_mom20/pool_vol20）只标注不过滤——ADR-16 红线，P1 存活清单显式排除。

用法（仓库根 cwd）：
    PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p0_features.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

OUT_DIR = "logs/quality"
STATE_PATH = "logs/r6_10_loop/state.json"


# ============================================================================
# 单符号富化：事件四元组 + 逐笔特征（几何/动量/量能/位置）
# ============================================================================
def _enrich_symbol(sym, sym_df, filled, event_map, id_cfg, exec_cfg):
    """单符号的 filled 记录 → 富化行列表。

    event_map: {signal_date(Timestamp): (i, (neckline, bottom, atr, formed_at))}
    所有特征只用 ≤ i 的数据（无前视）；窗口几何按内核同口径复刻。
    """
    window = id_cfg["window"]
    highs = sym_df["high"].to_numpy(dtype=float)
    lows = sym_df["low"].to_numpy(dtype=float)
    closes = sym_df["close"].to_numpy(dtype=float)
    vols = sym_df["volume"].to_numpy(dtype=float)
    index = sym_df.index
    n = len(sym_df)

    from strategies.neckline.method_v0 import (TOPS_WINDOW, compute_atr,
                                               decay_weights_of,
                                               local_extrema_mask)
    atr_full = compute_atr(sym_df["high"], sym_df["low"], sym_df["close"],
                           window=window).to_numpy(dtype=float)
    tau = id_cfg.get("decay_tau")
    decay_w = decay_weights_of(window, tau) if (tau and tau > 0) else None
    lew = id_cfg["local_extrema_window"]
    max_wait = exec_cfg["max_wait"]
    stop_mult = id_cfg["stop_atr_mult"]
    tp_mult = id_cfg["tp_h_mult"]

    date_pos = {pd.Timestamp(d): k for k, d in enumerate(index)}
    rows = []
    for r in filled:
        sd = pd.Timestamp(r["signal_date"])
        ev = event_map.get(sd)
        if ev is None:
            continue          # 无事件四元组（防御：不应发生，丢弃并计数见外层）
        i, (c_star, bottom, atr_val, _formed) = ev
        H = c_star - bottom
        entry = r.get("entry")
        row = {
            "symbol": sym,
            "signal_date": sd,
            "buy_date": pd.Timestamp(r["buy_date"]) if r.get("buy_date") else None,
            "exit_date": pd.Timestamp(r["exit_date"]) if r.get("exit_date") else None,
            "year": sd.year,
            # —— y ——
            "avg_pnl_pct": r["avg_pnl_pct"],
            "lot1_pnl_pct": r.get("lot1_pnl_pct"),
            "lot2_pnl_pct": r.get("lot2_pnl_pct"),
            "exit_reason": r["exit_reason"],
            "win": int(float(r["avg_pnl_pct"]) > 0),
            "holding_bars": r.get("holding_bars"),
            # —— 几何族（at_signal）——
            "h_atr": r.get("H_over_ATR"),
            "H": round(H, 4) if H > 0 else None,
            "atr": round(float(atr_val), 4),
            "rr_id": round(tp_mult * H / (stop_mult * atr_val), 3)
                     if H > 0 and atr_val > 0 else None,
            # —— 透传（识别/执行档案）——
            "neckline": r.get("neckline"),
            "entry": entry,
            "tp2": r.get("tp2"),
            "risk_pct": r.get("risk_pct"),
            "stop_gap": r.get("stop_gap"),
            "same_day_both": r.get("same_day_both"),
        }
        # rr_exec（成交价口径）：tp2 已含 B3 锚自适应；base_stop=颈线−stop_mult×ATR
        if entry is not None and H > 0 and atr_val > 0:
            base_stop = c_star - stop_mult * atr_val
            risk_d = float(entry) - base_stop
            row["rr_exec"] = round((float(r["tp2"]) - float(entry)) / risk_d, 3) if risk_d > 0 else None
        else:
            row["rr_exec"] = None

        # 窗口切片几何复刻（触及数/底部离散度/压制度/形态时长）
        s = i - window + 1
        if s >= 0:
            high_w = highs[s:i + 1]
            low_w = lows[s:i + 1]
            close_w = closes[s:i + 1]
            tops_mask = local_extrema_mask(high_w, TOPS_WINDOW, kind="max")
            tops_pos = np.flatnonzero(tops_mask)
            if len(tops_pos):
                tvals = high_w[tops_pos]
                in_band = np.abs(tvals - c_star) <= atr_val
                row["touches"] = int(in_band.sum())
                row["n_tops"] = int(len(tvals))
                band_pos = tops_pos[in_band]
                row["pattern_days"] = int(len(high_w) - 1 - band_pos.min())   # 最早聚集顶→突破日
                row["neckline_span"] = int(band_pos.max() - band_pos.min())  # 颈线带时间跨度
            else:
                row["touches"] = row["n_tops"] = 0
                row["pattern_days"] = row["neckline_span"] = None
            lows_mask = local_extrema_mask(low_w, lew, kind="min")
            lvals = low_w[np.flatnonzero(lows_mask)]
            min_p = float(np.nanmin(low_w))
            bset = lvals[(lvals >= min_p) & (lvals <= min_p + atr_val)]
            row["bottom_disp"] = round(float(bset.max() - min_p) / atr_val, 3) if len(bset) else None
            below = close_w < c_star
            if decay_w is not None:
                row["suppression"] = round(float(decay_w[below].sum() / decay_w.sum()), 3)
            else:
                row["suppression"] = round(float(below.sum() / len(close_w)), 3)
        else:
            for k in ("touches", "n_tops", "bottom_disp", "suppression"):
                row[k] = None
            row["pattern_days"] = row["neckline_span"] = None

        # —— 动量族（at_signal，≤i 无前视）——
        row["ret5"] = closes[i] / closes[i - 5] - 1.0 if i >= 5 else None
        row["ret20"] = closes[i] / closes[i - 20] - 1.0 if i >= 20 else None
        row["ret60"] = closes[i] / closes[i - 60] - 1.0 if i >= 60 else None
        if i >= 59:
            row["pos_high60"] = closes[i] / float(np.nanmax(highs[i - 59:i + 1])) - 1.0
        else:
            row["pos_high60"] = None
        row["breakout_ret"] = closes[i] / closes[i - 1] - 1.0 if i >= 1 else None
        a_w = atr_full[max(0, i - 249):i + 1]
        a_w = a_w[~np.isnan(a_w)]
        row["atr_pct"] = round(float((a_w <= atr_val).mean()), 4) if len(a_w) >= 60 else None

        # —— 量能族（at_signal）——
        row["bvr"] = r.get("breakout_vol_ratio")
        if i >= 7:
            m1 = float(np.nanmean(vols[i - 2:i + 1]))
            m0 = float(np.nanmean(vols[i - 7:i - 3]))
            row["vol5_slope"] = round(m1 / m0 - 1.0, 3) if m0 > 0 else None
        else:
            row["vol5_slope"] = None
        if i >= 9:
            c10 = closes[i - 9:i + 1]
            v10 = vols[i - 9:i + 1]
            rc = np.argsort(np.argsort(c10)).astype(float)
            rv = np.argsort(np.argsort(v10)).astype(float)
            if rc.std() > 0 and rv.std() > 0:
                row["pv_corr10"] = round(float(np.corrcoef(rc, rv)[0, 1]), 3)
            else:
                row["pv_corr10"] = None
        else:
            row["pv_corr10"] = None

        # —— 位置族（at_fill：成交后才可知——只供 P1 归因，禁入 P2 分层分）——
        if atr_val > 0 and entry is not None:
            row["entry_depth_atr"] = round((float(entry) - c_star) / atr_val, 3)
        else:
            row["entry_depth_atr"] = None
        bd = r.get("buy_date")
        if bd is not None:
            bi = date_pos.get(pd.Timestamp(bd))
            row["wait_days"] = int(bi - i) if bi is not None else None
        else:
            row["wait_days"] = None
        row["is_chase"] = int(row["wait_days"] is not None and row["wait_days"] > max_wait)
        rows.append(row)
    return rows


def _scan_pool(pool_tag, params, universe, udates=None):
    """一个池（今日截面 / wf 折）的扫描+富化。返回 (rows, n_outer_anchor_filled)。

    复刻 run_full_scan 语义（id_cfg/exec_cfg 构造逐行同源）+ 同迭代事件元组捕获。
    """
    from discovery.objective import EXEC_KEYS, ID_KEYS
    from strategies.neckline import backtest as bk
    from strategies.neckline.method_v0 import DEFAULTS

    id_cfg = {**DEFAULTS, **{k: params.get(k, DEFAULTS.get(k)) for k in ID_KEYS}}
    exec_cfg = {**bk.EXEC_DEFAULTS,
                **{k: params.get(k, bk.EXEC_DEFAULTS.get(k)) for k in EXEC_KEYS}}
    window = id_cfg["window"]

    # 环境族（只标注）：池子等权 20 日动量/波动
    from discovery.manual_risk_sim import _pool_equity
    pool_eq = _pool_equity(universe)
    mom20 = (pool_eq / pool_eq.shift(20) - 1.0)
    vol20 = pool_eq.pct_change().rolling(20).std()
    mom_map = {pd.Timestamp(d): float(v) for d, v in mom20.dropna().items()}
    vol_map = {pd.Timestamp(d): float(v) for d, v in vol20.dropna().items()}
    _mkeys = sorted(mom_map)
    _vkeys = sorted(vol_map)

    def _lookup(m, keys, d):
        import bisect
        k = bisect.bisect_right(keys, d) - 1
        return m[keys[k]] if k >= 0 else None

    all_rows, n_fail, n_no_event = [], 0, 0
    syms = sorted(universe)
    t0 = time.time()
    for cnt, sym in enumerate(syms):
        sym_df = universe[sym]
        try:
            filled, _ns, _nsk = bk.scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)
            events = bk._cached_identify_events(sym_df, id_cfg)   # 同迭代必命中
        except Exception:
            n_fail += 1
            continue
        if not filled:
            continue
        event_map = {pd.Timestamp(sym_df.index[i]): (i, ev) for i, ev in events}
        rows = _enrich_symbol(sym, sym_df, filled, event_map, id_cfg, exec_cfg)
        n_no_event += len(filled) - len(rows)
        all_rows.extend(rows)
        if (cnt + 1) % 300 == 0:
            print(f"[{pool_tag}] {cnt + 1}/{len(syms)} 符号 {len(all_rows)} 笔 "
                  f"({time.time() - t0:.0f}s)", flush=True)

    # same_day_n（at_signal：EOD 全池信号已知）+ 环境族标注
    df = pd.DataFrame(all_rows)
    if len(df):
        cnt_by_day = df.groupby(df["signal_date"]).size()
        df["same_day_n"] = df["signal_date"].map(cnt_by_day).astype(int)
        df["pool_mom20"] = df["signal_date"].map(
            lambda d: _lookup(mom_map, _mkeys, pd.Timestamp(d)))
        df["pool_vol20"] = df["signal_date"].map(
            lambda d: _lookup(vol_map, _vkeys, pd.Timestamp(d)))
        df["pool"] = pool_tag
    print(f"[{pool_tag}] 完成：{len(df)} 笔 / 失败符号 {n_fail} / 丢事件笔 {n_no_event} "
          f"({time.time() - t0:.0f}s)", flush=True)
    return df, n_fail


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from discovery.snapshot import freeze, load_universe_window
    from discovery.split import holdout_split, walk_forward_split
    from discovery.objective import portfolio_metrics
    from backtest.models import PositionModel

    base = json.load(open(STATE_PATH, encoding="utf-8"))["base"]

    # —— 池 1：今日截面（对照线口径 universe）——
    print("[main] freeze('2021-01-01') ...", flush=True)
    t0 = time.time()
    universe, meta = freeze("2021-01-01")
    udates = next(iter(universe.values())).index
    print(f"[main] universe={meta.universe_count} hash={meta.snapshot_hash} "
          f"range={meta.date_range} ({time.time() - t0:.0f}s)", flush=True)

    df_main, n_fail = _scan_pool("main", base, universe)

    # —— 锚验证：outer 2026 × 冻结口径复现 r6_11c ——
    # 湖是活的（18:00 EOD 每日落新 K 线；r6_11c 今晨跑时湖尾=08-25，本次已含 08-26）。
    # 严格对拍用「截尾到 08-25」（r6_11c 的数据视野），全量读数另报（数据前进 delta）。
    split = holdout_split()
    LAKE_CUT = pd.Timestamp("2026-08-25")   # r6_11c 基线的数据尾
    _trim = df_main[(df_main["signal_date"].apply(split.outer.covers))
                    & (df_main["signal_date"] <= LAKE_CUT)]
    filled_anchor = [{"symbol": r["symbol"], "signal_date": r["signal_date"],
                      "buy_date": r["buy_date"], "exit_date": r["exit_date"],
                      "avg_pnl_pct": r["avg_pnl_pct"], "entry": r["entry"]}
                     for _, r in _trim.iterrows()]
    _fresh = df_main[df_main["signal_date"] > LAKE_CUT]
    print(f"[anchor] 截尾笔数={len(filled_anchor)}（08-26 新增 "
          f"{len(_fresh)} 笔=湖前进 delta，非口径差）", flush=True)
    pm_freeze = PositionModel(capital=1_000_000, lot_size=100, min_fee=5.0,
                              max_positions=4, pos_cap=0.075, freeze_pending=True)
    anchor = portfolio_metrics(filled_anchor, split.outer, udates, position_model=pm_freeze)
    _full_outer = df_main[df_main["signal_date"].apply(split.outer.covers)]
    _full_filled = [{"symbol": r["symbol"], "signal_date": r["signal_date"],
                     "buy_date": r["buy_date"], "exit_date": r["exit_date"],
                     "avg_pnl_pct": r["avg_pnl_pct"], "entry": r["entry"]}
                    for _, r in _full_outer.iterrows()]
    anchor_full = portfolio_metrics(_full_filled, split.outer, udates,
                                    position_model=pm_freeze)
    print(f"[anchor] 截尾(≤08-25): n_outer={len(filled_anchor)} "
          f"taken={anchor['n_taken']} ann={anchor['ann']:+.3f} "
          f"win={anchor['win_rate']:.3f}", flush=True)
    print(f"[anchor] 全量(含08-26): n_outer={len(_full_filled)} "
          f"taken={anchor_full['n_taken']} ann={anchor_full['ann']:+.3f} "
          f"win={anchor_full['win_rate']:.3f}", flush=True)
    ok = (anchor["n_taken"] == 445
          and abs(anchor["ann"] - 4.007) < 0.03
          and abs(anchor["win_rate"] - 0.760) < 0.006)
    if not ok:
        print("[anchor][FAIL] 与同湖原脚本基准（445/+400.7%/0.760，见下）不吻合"
              "——中止，特征库不落盘", flush=True)
        sys.exit(2)
    # 锚口径说明（2026-08-26 实测）：r6_11c 今晨 442/+399.8%/75.8% 在当前湖不可
    # 复现——18:00 EOD 同步使湖尾 08-25→08-26 + qfq 前进。决定性证伪=原样重跑
    # diag/r6_11c_freeze.py（run_full_scan 同路径）于当前湖：445 taken/+400.7%/76%。
    # 本管线同湖对拍：外层笔集逐位一致（11520/445/0.760），ann 尾差为年化分母级
    # ——管线等价成立，+400% 对照线口径不变（读数随湖版本微幅前进）。

    # —— 池 2-5：wf 四折诚实池 OOS 段（P1 的 Q3 重证数据）——
    wf = walk_forward_split()
    fold_frames = []
    for name, train, oos in wf.folds:
        print(f"\n[{name}] load_universe_window({train.start}~{train.end}, "
              f"data_end={oos.end}) ...", flush=True)
        t1 = time.time()
        uni_f = load_universe_window(train.start, train.end, data_end=oos.end,
                                     warmup_days=400)
        print(f"[{name}] universe={len(uni_f)} ({time.time() - t1:.0f}s)", flush=True)
        df_f, _ = _scan_pool(name, base, uni_f)
        if len(df_f):
            from datetime import timedelta
            cut = oos.start + timedelta(days=wf.embargo_days)
            df_f = df_f[df_f["signal_date"].apply(oos.covers)].copy()
            df_f["embargoed"] = df_f["signal_date"] < pd.Timestamp(cut)
            df_f["fold"] = name
        fold_frames.append(df_f)

    df_all = pd.concat([df_main] + fold_frames, ignore_index=True)
    # 主池行无 embargoed 键（concat 填 NaN）→ 统一 bool（下游 ~mask 对 NaN 会炸）
    df_all["embargoed"] = df_all["embargoed"].fillna(False).astype(bool)
    out_parquet = os.path.join(OUT_DIR, "trades_features.parquet")
    df_all.to_parquet(out_parquet, index=False)

    # —— meta：口径/锚/可用性分类（P1/P2 消费契约）——
    feature_avail = {
        "at_signal": ["h_atr", "H", "atr", "rr_id", "touches", "n_tops",
                      "bottom_disp", "pattern_days", "neckline_span",
                      "suppression", "ret5", "ret20", "ret60", "pos_high60",
                      "breakout_ret", "atr_pct", "bvr", "vol5_slope",
                      "pv_corr10", "same_day_n"],
        "at_fill_analysis_only": ["rr_exec", "entry_depth_atr", "wait_days",
                                  "is_chase"],
        "env_label_only_adr16": ["pool_mom20", "pool_vol20"],
        "y": ["avg_pnl_pct", "lot1_pnl_pct", "lot2_pnl_pct", "exit_reason",
              "win", "holding_bars"],
        "note": ("at_signal 列表才是 P2 分层分合法特征域（signal_date 收盘可知）；"
                 "at_fill 类依赖成交价/成交日，仅 P1 归因用；环境族 ADR-16 只标注"
                 "不过滤不进任何决策路径"),
    }
    seg_counts = {p: int(n) for p, n in df_all.groupby("pool").size().items()}
    year_main = {int(y): int(n) for y, n in
                 df_main.groupby(df_main["signal_date"].dt.year).size().items()}
    meta_out = {
        "params": base,
        "universe_snapshot": {"hash": meta.snapshot_hash,
                              "count": meta.universe_count,
                              "range": meta.date_range,
                              "lake_start": "2021-01-01"},
        "anchor_outer2026_freeze": {
            "n_taken": anchor["n_taken"], "ann": round(anchor["ann"], 4),
            "win": round(anchor["win_rate"], 4),
            "n_outer": len(filled_anchor),
            "lake_cut": "2026-08-25",
            "reproduces": "原脚本同湖对拍 445/+400.7%/0.760（r6_11c_freeze.py 重跑）",
            "drift_note": ("r6_11c 今晨 442/+399.8%/75.8% 在当前湖不可复现：18:00 "
                           "EOD 同步使湖尾 08-25→08-26 + qfq 前进；外层笔集两管线"
                           "逐位一致（11520/445/76.0%）——数据版本漂移非口径漂移"),
            "r6_11c_morning_ref": {"n_taken": 442, "ann": 3.998, "win": 0.758},
            "full_incl_0826": {"n_taken": anchor_full["n_taken"],
                               "ann": round(anchor_full["ann"], 4),
                               "win": round(anchor_full["win_rate"], 4),
                               "n_outer": len(_full_filled)},
        },
        "pools": seg_counts,
        "main_by_year": year_main,
        "feature_avail": feature_avail,
        "wf_folds": [{"fold": n, "train": [str(t.start), str(t.end)],
                      "oos": [str(o.start), str(o.end)]} for n, t, o in wf.folds],
        "n_scan_fail_symbols_main": n_fail,
        "generated_at": pd.Timestamp.now().isoformat(),
    }
    with open(os.path.join(OUT_DIR, "features_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta_out, f, ensure_ascii=False, indent=1, default=str)

    print(f"\n[done] {out_parquet} rows={len(df_all)}", flush=True)
    print(json.dumps(seg_counts, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
