# -*- coding: utf-8 -*-
"""R6-1 识别/执行解耦缓存（2026-08-26）三层等价守护。

物理意图：scan_symbol 拆两段（段 1 识别循环 exec 无关化 + 三键缓存；段 2 cancel
守卫重放 + dedup + simulate_exit）后，必须证明拆分前后输出**逐位一致**——识别/
执行解耦是 R6 排程第 1 项，任何一位漂移都会让 98 项真值图谱（logs/r6a_rescan.json）
的后续读数与已定稿读数不可比。

三层守护：
  ① 拆分 == 一体化参考实现（_monolithic_ref 逐行复刻拆分前 scan_symbol，真实
     识别内核 + 真实数据，id_cfg/exec 多变体含 cancel 档对拍）；
  ② 缓存命中路径 == 无缓存路径（同 id_cfg：冷跑 exec A → 换 exec B 灌缓存 → 再跑
     exec A 必须逐位复现冷跑）；
  ③ 同 id_cfg 异 exec 二次评估命中缓存（miss/hit 计数器断言 + 键敏感性：id_cfg
     变/数据变 → miss；exec 变 → hit）。

契约注记（有意为之，非遗漏）：cancel_on close 守卫从识别内核内（_post_detect）
移到 scan_symbol 段 2 重放——生产路径逐位等价（①守护），但 monkeypatch 掉
detect_signal_fast 的测试桩不再自动绕过守卫（守卫在编排层补一道）。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

# 项目根挂 sys.path（与 tests/test_param_iter_kernel_same_source.py 同风格）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline import backtest as bk  # noqa: E402
from strategies.neckline.method_v0 import (  # noqa: E402
    DEFAULTS, TOPS_WINDOW, compute_atr, decay_weights_of, detect_signal_fast,
    local_extrema_mask)


# 每个测试前清缓存：进程级缓存跨测试泄漏会让 miss/hit 断言失真（合成数据若与
# 其他测试同内容同 id_cfg，会拿到别人的缓存条目）。
@pytest.fixture(autouse=True)
def _fresh_cache():
    bk._clear_scan_id_cache()
    yield
    bk._clear_scan_id_cache()


def _monolithic_ref(sym_df, window, exec=None, id_cfg=None):
    """拆分前 scan_symbol 的逐行参考实现（等价性对拍的对照真值）。

    与 R6-1 拆分前 git 版本逐行同构：识别循环内联 detect_signal_fast(**真 exec**)
    ——cancel_on close 守卫在 _post_detect 内核内生效——→ dedup → simulate_exit。
    本函数不走缓存、不 patch 内核，是与新 scan_symbol 对拍的唯一真值。
    """
    if exec is None:
        exec = bk.EXEC_DEFAULTS
    if id_cfg is None:
        id_cfg = {**DEFAULTS, "window": window}
    atr_full = compute_atr(sym_df["high"], sym_df["low"], sym_df["close"],
                           window=id_cfg["window"])
    arr = {
        "high": sym_df["high"].to_numpy(),
        "low": sym_df["low"].to_numpy(),
        "close": sym_df["close"].to_numpy(),
        "volume": sym_df["volume"].to_numpy(),
        "index": sym_df.index,
    }
    atr_arr = atr_full.to_numpy()
    tops_mask = local_extrema_mask(arr["high"], TOPS_WINDOW, kind="max")
    lows_mask = local_extrema_mask(arr["low"], id_cfg["local_extrema_window"], kind="min")
    tau = id_cfg.get("decay_tau")
    decay_weights = None
    if tau and tau > 0:
        decay_weights = decay_weights_of(id_cfg["window"], tau)

    signals = []
    for i in range(id_cfg["window"], len(sym_df)):
        sig = detect_signal_fast(None, arr, i, id_cfg, exec, sym_df.index[i], atr_arr,
                                 tops_mask=tops_mask, lows_mask=lows_mask,
                                 decay_weights=decay_weights)
        if sig is not None:
            signals.append((i, sig))
    signals = bk.dedup_signals(signals, cooldown=exec["cooldown"])
    filled = []
    n_skip = 0
    for sig_idx, sig in signals:
        sim = bk.simulate_exit(sym_df, sig_idx, sig.neckline, sig.bottom, sig.atr,
                               exec=exec, id_cfg=id_cfg)
        if sim is None:
            continue
        if sim["exit_reason"] in ("skip_no_pullback", "skip_target_met"):
            n_skip += 1
        else:
            vol_T = float(sym_df["volume"].iloc[sig_idx])
            vol5 = (float(sym_df["volume"].iloc[max(0, sig_idx - 5):sig_idx].mean())
                    if sig_idx >= 5 else vol_T)
            sim["breakout_vol_ratio"] = round(vol_T / vol5, 2) if vol5 > 0 else 0.0
            sim["suppression"] = None
            filled.append(sim)
    return filled, len(signals), n_skip


def _load_lake_symbols(start="2024-01-01", candidates=None, min_signals=2, take=2):
    """从数据湖取 ≥min_signals 信号的标的（无湖/无信号则 skip——CI 无数据环境）。

    2024 起（640 根）：默认参数下流动性头部标的有 2-4 信号（688041.SH 4 信号全成
    交 / 300308.SZ 2 信号含 skip 路径），足够驱动守卫/去重/模拟全链路对拍。
    """
    lake_path = _ROOT / "data_lake" / "a_shares_daily.parquet"
    if not lake_path.exists():
        pytest.skip("data_lake 缺失，跳过真实数据等价守护（CI 无数据环境）")
    lake = pd.read_parquet(lake_path, filters=[("date", ">=", pd.Timestamp(start))])
    out = []
    for sym in (candidates or ["688041.SH", "300308.SZ", "688008.SH", "300058.SZ"]):
        try:
            sym_df = lake.xs(sym, level="symbol").sort_index()
        except Exception:
            continue
        _, n_sig, _ = bk.scan_symbol(sym_df, DEFAULTS["window"])
        if n_sig >= min_signals:
            out.append((sym, sym_df))
        if len(out) >= take:
            break
    return out


# ============================================================================
# ① 拆分 == 一体化参考实现（真实内核 + 真实数据，逐位对拍）
# ============================================================================
def test_split_matches_monolithic_replica_real_data():
    """新 scan_symbol（两段式+缓存）== 拆分前参考实现，逐位一致（含 cancel 档）。

    覆盖面：默认 exec（cancel=1.0）/ cancel=None + 多 exec 键 / cancel=2.0 +
    buy_limit=2.5 + trailing / id_cfg window=100 / id_cfg decay_tau=90——id_cfg 与
    exec 双层变体，证段 1 缓存剥离（entry/exec_params）与段 2 重放（cancel 守卫/
    cooldown 去重/价位单源）两侧合流后与一体化路径零漂移。
    """
    pairs = _load_lake_symbols()
    assert pairs, "候选标的全无信号，测试无效（数据环境异常）"
    exec_variants = [
        None,                                                    # EXEC_DEFAULTS（cancel=1.0）
        {"cancel_thresh_mult": None, "cooldown": 8, "tp1_portion": 0.9,
         "max_holding": 30},                                     # 守卫关 + 去重/止盈/持有全变
        {"cancel_thresh_mult": 2.0, "buy_limit_atr_mult": 2.5,
         "trailing_grace": 5, "trailing_step": 0.1},             # 守卫阈值变 + 挂单/trailing 变
    ]
    id_variants = [
        None,                                                    # {**DEFAULTS, window:window}
        {"window": 100},
        {"decay_tau": 90},
    ]
    checked = 0
    for sym, sym_df in pairs:
        for id_ov in id_variants:
            id_cfg = {**DEFAULTS, "window": DEFAULTS["window"], **id_ov} if id_ov else None
            for exec_ov in exec_variants:
                exec_cfg = {**bk.EXEC_DEFAULTS, **exec_ov} if exec_ov else None
                got = bk.scan_symbol(sym_df, DEFAULTS["window"], exec=exec_cfg, id_cfg=id_cfg)
                ref = _monolithic_ref(sym_df, DEFAULTS["window"], exec=exec_cfg, id_cfg=id_cfg)
                assert got == ref, (
                    f"{sym} id_ov={id_ov} exec_ov={exec_ov} 拆分前后输出漂移：\n"
                    f"got n={got[1]}/skip={got[2]} ref n={ref[1]}/skip={ref[2]}"
                )
                checked += 1
    # 守护测试自身有效性：至少跑过 2 标的 × 9 组合
    assert checked >= 18


# ============================================================================
# ② 缓存命中路径 == 无缓存路径
# ============================================================================
def test_cache_hit_path_equals_cold_path_real_data():
    """冷跑 exec A → 换 exec B 灌缓存 → 再跑 exec A：两次 A 逐位一致。

    这是「缓存条目在 exec 变化间被复用时无污染」的直接证据——段 1 事件被 exec B
    的评估读过之后，exec A 再读仍是自己的真值（键不含 exec，正确性全靠事件
    exec 无关性）。
    """
    pairs = _load_lake_symbols()
    assert pairs
    sym, sym_df = pairs[0]
    exec_a = {**bk.EXEC_DEFAULTS, "cancel_thresh_mult": 2.0}
    exec_b = {**bk.EXEC_DEFAULTS, "max_holding": 40, "cooldown": 8,
              "buy_limit_atr_mult": 3.0, "tp1_portion": 0.9}
    bk._clear_scan_id_cache()   # 选标探针已灌缓存，清后重置冷/热边界
    cold = bk.scan_symbol(sym_df, DEFAULTS["window"], exec=exec_a)
    assert bk._scan_id_cache_stats["miss"] == 1, "冷跑应恰好一次 miss"
    bk.scan_symbol(sym_df, DEFAULTS["window"], exec=exec_b)
    warm = bk.scan_symbol(sym_df, DEFAULTS["window"], exec=exec_a)
    assert warm == cold, "缓存命中路径与无缓存路径输出漂移"


# ============================================================================
# ③ 命中/键敏感性（合成数据 + mock 识别，快速确定性）
# ============================================================================
def _synth_df(close_at_signal=102.0, n=80):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    base = {"open": 9.8, "high": 10.0, "low": 9.0, "close": 9.5,
            "volume": 1000, "amount": 10000}
    df = pd.DataFrame(base, index=idx)
    df.iloc[60, df.columns.get_loc("close")] = close_at_signal   # 信号日 close（cancel 守卫读）
    return df


def _mock_detect(monkeypatch, sig_pos=60, neckline=100.0, bottom=90.0, atr=3.6):
    """patch bk.detect_signal_fast：仅在 sig_pos 返合成 Signal（几何可配，喂守卫重放）。"""
    from strategies.neckline.signal import Signal
    holder = {"calls": 0}

    def fake_fast(symbol, arr, pos, id_cfg, exec_cfg, date, atr_arr,
                  tops_mask=None, lows_mask=None, decay_weights=None):
        holder["calls"] += 1
        if pos != sig_pos:
            return None
        return Signal(symbol=None, formed_at=arr["index"][pos],
                      breakout_date=arr["index"][pos],
                      neckline=neckline, bottom=bottom, atr=atr)

    monkeypatch.setattr(bk, "detect_signal_fast", fake_fast)
    return holder


def test_same_id_cfg_diff_exec_hits_cache(monkeypatch):
    """同 id_cfg 换 exec 二次评估命中缓存（识别内核只跑一次）。

    R6-1 提速命题的核心断言：exec 全维变化（max_holding/cooldown/tp1 组/cancel/
    buy_limit/trailing）不触发识别重跑；反之 id_cfg 任一键变、数据内容变都
    必须 miss（键敏感性）。
    """
    df = _synth_df()
    holder = _mock_detect(monkeypatch)
    id_cfg = {**DEFAULTS, "window": 20}
    exec_variants = [
        {**bk.EXEC_DEFAULTS},
        {**bk.EXEC_DEFAULTS, "max_holding": 40, "cooldown": 8},
        {**bk.EXEC_DEFAULTS, "tp1_portion": 0.9, "cancel_thresh_mult": 2.0,
         "buy_limit_atr_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1},
    ]
    outs = [bk.scan_symbol(df, 20, exec=e, id_cfg=id_cfg) for e in exec_variants]
    st = bk._scan_id_cache_state()
    assert st["miss"] == 1 and st["hit"] == 2, f"exec 变体应全命中: {st}"
    assert holder["calls"] >= 1, "mock 未被调用（测试无效）"
    calls_after_execs = holder["calls"]

    # id_cfg 变（window 20→30）→ miss；识别循环真重跑
    bk.scan_symbol(df, 20, exec=exec_variants[0], id_cfg={**DEFAULTS, "window": 30})
    assert bk._scan_id_cache_stats["miss"] == 2
    assert holder["calls"] > calls_after_execs

    # 数据内容变（一根 close）→ miss
    df2 = df.copy()
    df2.iloc[10, df2.columns.get_loc("close")] = 9.7
    bk.scan_symbol(df2, 20, exec=exec_variants[0], id_cfg=id_cfg)
    assert bk._scan_id_cache_stats["miss"] == 3

    # 同参复跑 → 输出与首跑逐位一致（命中路径确定性）
    assert bk.scan_symbol(df, 20, exec=exec_variants[1], id_cfg=id_cfg) == outs[1]


def test_cancel_guard_replay_drop_and_keep(monkeypatch):
    """段 2 cancel 守卫重放：close≥cancel_on 丢、cancel=None 放飞。

    契约注记：守卫原在 _post_detect 内核内，R6-1 移到编排层重放（生产路径逐位
    等价由真实数据对拍守护）——mock 识别的测试里守卫依然生效（有意行为）。
    """
    df = _synth_df(close_at_signal=111.0)   # neckline=100, bottom=90 → H=10, cancel_on=110
    _mock_detect(monkeypatch)
    id_cfg = {**DEFAULTS, "window": 20}
    # cancel=1.0（默认）→ close 111 ≥ 110 丢
    filled, n_sig, _ = bk.scan_symbol(df, 20, id_cfg=id_cfg)
    assert n_sig == 0 and filled == []
    # cancel=2.0 → cancel_on=120 > 111 放行 → 模拟出场走通
    filled2, n_sig2, _ = bk.scan_symbol(
        df, 20, exec={**bk.EXEC_DEFAULTS, "cancel_thresh_mult": 2.0}, id_cfg=id_cfg)
    assert n_sig2 == 1 and len(filled2) == 1
    # cancel=None（不撤单放飞）→ 同样放行
    filled3, n_sig3, _ = bk.scan_symbol(
        df, 20, exec={**bk.EXEC_DEFAULTS, "cancel_thresh_mult": None}, id_cfg=id_cfg)
    assert n_sig3 == 1 and filled3 == filled2


def test_env_killswitch_bypasses_cache(monkeypatch):
    """NECKLINE_ID_CACHE=off 零代码旁路：不走缓存（对照/回滚口），行为不变。"""
    monkeypatch.setenv("NECKLINE_ID_CACHE", "off")
    df = _synth_df()
    _mock_detect(monkeypatch)
    id_cfg = {**DEFAULTS, "window": 20}
    out1 = bk.scan_symbol(df, 20, id_cfg=id_cfg)
    out2 = bk.scan_symbol(df, 20, id_cfg=id_cfg)
    st = bk._scan_id_cache_state()
    assert st["count"] == 0 and st["hit"] == 0 and st["miss"] == 0, (
        f"kill-switch 下不应有缓存活动: {st}")
    assert out1 == out2


def test_cache_eviction_bounded(monkeypatch):
    """总量有界驱逐：超 _ID_CACHE_MAX_EVENTS 从最老条目 FIFO 逐出（防长跑无界增长）。"""
    monkeypatch.setattr(bk, "_ID_CACHE_MAX_EVENTS", 6)
    bk._id_cache_put(("k1", "d1"), [(i, (1.0, 2.0, 3.0, None)) for i in range(5)])
    assert bk._scan_id_cache_n_events == 5
    bk._id_cache_put(("k2", "d2"), [(i, (1.0, 2.0, 3.0, None)) for i in range(5)])
    # 10 > 6 → 最老 k1 逐出，剩 k2 的 5 条
    assert ("k1", "d1") not in bk._scan_id_cache
    assert ("k2", "d2") in bk._scan_id_cache
    assert bk._scan_id_cache_n_events == 5
    assert bk._scan_id_cache_stats["evict"] == 1


def test_mock_events_do_not_pollute_real_kernel_cache(monkeypatch):
    """内核身份入键：mock 桩缓存与真内核缓存互不污染（qualname 换桩自动换键）。"""
    df = _synth_df()
    _mock_detect(monkeypatch)
    bk.scan_symbol(df, 20, id_cfg={**DEFAULTS, "window": 20})   # mock 桩路径，miss=1
    assert bk._scan_id_cache_stats["miss"] == 1
    monkeypatch.undo()                                          # 还原真内核 + 清缓存
    bk._clear_scan_id_cache()
    # 真内核同数据同 id_cfg → 新键（qualname 不同）→ miss 而非吃到 mock 缓存
    bk.scan_symbol(df, 20, id_cfg={**DEFAULTS, "window": 20})
    st = bk._scan_id_cache_state()
    assert st["miss"] == 1 and st["hit"] == 0, (
        f"真内核不应命中 mock 桩的缓存条目: {st}")
