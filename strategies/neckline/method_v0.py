# -*- coding: utf-8 -*-
"""颈线法形态识别器 v0（最小版 · 逻辑验证用）。

物理定位：
    对 caisen 现行"拐点法"形态识别的范式替代实验——不依赖 zigzag 拐点提取，
    而是以颈线为核心、以价格聚集带为语言识别底部形态。

核心判定流程（压实后参数，零待定）：
    ① 窗口 W = 近 N 日（默认 60；20-120 区间待 replay 定标）
    ② 颈线 = 窗口内【顶部高点聚集】的价位（顶点连线定位）+【压制时长】验证
       （close<颈线的比例 ≥ min_suppression，价格长期被压在颈线下方才有效）
    ③ 底部 = 窗口最低点 min + [min, min+ATR] 内的离散局部极值低点（含 min ≥2 个）
    ④ 突破 = 末根收盘 close > 颈线 c*（信号触发）
    ⑤ 进场 = 颈线价 c*（挂单等回踩；close>c* 只触发信号，不追涨）
    ⑥ R3 实际口径盈亏比 rr = (tp2−entry)/(entry−stop_price)
       （stop_price = 颈线 − N×ATR，与 execute 层 base_stop 同口径；min_rr 验真实盈亏比）

交易要素（用户规则，持有期模拟见 neckline_backtest.py）：
    进场执行 = T+1 日收盘买入；止损 = 颈线 c*；止盈 = 50%@颈线+H，50%@颈线+2H；
    超时 = 15 日未达止盈收盘卖剩余。

风控边界（CLAUDE.md 极简 + 显式 + 防御性）：
    - 数据不足（< 窗口）/ ATR 无效 / 颈线或谷底异常 → 显式返 None；
    - 局部极值用左右各 w 根比较，排除窗口边界 w 根；
    - 窗口最低点强制纳入底部集合（anchor）。

用法：
    PYTHONIOENCODING=utf-8 python -u strategies/neckline/method_v0.py
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
# P1（2026-08-13 · spec 2026-08-12-overall-optimization-design §2）：识别热路径向量化——
# local_extrema_mask 用滑动窗视图一次算全序列局部极值掩码（替代逐日窗口 Python 循环），
# numpy>=1.24 已锁（requirements.txt），零新增依赖。
from numpy.lib.stride_tricks import sliding_window_view

# Signal dataclass（Task 1 归位 strategies/neckline/signal.py）——detect_signal 装配
# 完整 Signal 返回。同包子相对 import（Layer2 Task 1.5 收口口径）。
from .signal import Signal


# ============================================================================
# 压实后的参数（replay 定标项用默认值起步）
# ============================================================================
DEFAULTS = {
    "window": 60,              # ① 窗口（20-120 区间，起步 60）
    "min_touches": 2,          # ② 颈线由 ≥2 个顶部高点聚集连成（定位用，不要求频繁）
    "min_suppression": 0.6,    #    压制时长下限：≥60% 的 close 在颈线下方才算有效
    "local_extrema_window": 3, # ③ 局部极值左右各 3 根
    "min_bottoms": 2,          #    至少双底（含 min 在内）
    "breakout_vol_mult": 1.5,  #    突破带量 1.5×近5日均量（复用 caisen）
    "min_rr": 1.5,             # ⑥ 实际盈亏比下限（rr 实际口径 (tp2−entry)/(entry−stop_price)，min_rr 验真实盈亏比）
    "max_h_atr": 4.0,          # ⑦ 形态深度上限 H/ATR（实证：浅形态胜率51% vs 深形态27%，深=暴跌反弹）
    "stop_atr_mult": 1.0,      # ⑧ 止损 ATR 倍数（止损=颈线−N×ATR；参数化供迭代）
    "tp_h_mult": 2.0,          # ⑨ 止盈2 的 H 倍数（止盈2=颈线+N×H；参数化供迭代）
    "decay_tau": None,                 # ⑩ 颈线聚集时间衰减（日 exp(-dt/tau)）
                                      #    方案A(2026-07-19)修颈线漂移(513130 0.812→0.750)✓ 但全市场净-3.2点
                                      #    (29.4%→26.2%)：中小盘+3点(top100~400 10.3→13.3)但大盘拖累更大
                                      #    (近期颈线=弱阻力，大盘控盘弱失效)。当前最优等权29.4%，暂回None。
                                      #    颈线漂移问题真实但纯时间衰减非正解，留作后续(量加权或其他)。
}


# ============================================================================
# 基元：ATR（自写避免依赖；原 caisen.patterns.zigzag_causal.compute_atr 同口径，
#       该模块已随 caisen 形态退役删除）
# ============================================================================
def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series,
                window: int = 14) -> pd.Series:
    """ATR = TR 的 window 日均值（因果，min_periods=1 防早期 NaN）。

    TR（真实波幅）= max(当日H-当日L, |当日H-昨收|, |当日L-昨收|)，含跳空缺口。
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=1).mean()


# ============================================================================
# 局部极小值 / 极大值：离散拐点提取（避免每日 low/high 连续值的多计）
# ============================================================================
def local_minima(values, w: int):
    """局部极小值：某点比左右各 w 根都低（≤）即一个离散低点。排除首尾各 w 根。"""
    n = len(values)
    mins = []
    for i in range(w, n - w):
        left = values[i - w:i]
        right = values[i + 1:i + w + 1]
        if values[i] <= left.min() and values[i] <= right.min():
            mins.append(float(values[i]))
    return mins


def local_maxima(values, w: int):
    """局部极大值：某点比左右各 w 根都高（≥）即一个顶部高点。排除首尾各 w 根。"""
    n = len(values)
    maxs = []
    for i in range(w, n - w):
        left = values[i - w:i]
        right = values[i + 1:i + w + 1]
        if values[i] >= left.max() and values[i] >= right.max():
            maxs.append(float(values[i]))
    return maxs


def local_extrema_mask(values, w: int, kind: str = "min") -> np.ndarray:
    """全序列局部极值布尔掩码（P1 向量化基元 · 与 local_minima/local_maxima 位置语义逐位一致）。

    kind="min"：某点 <= 左右各 w 根（局部极小，对齐 local_minima）；kind="max"：>= 左右
    各 w 根（局部极大，对齐 local_maxima / search_neckline 内联 tops 检测）。True 位 =
    旧逐点循环会收集的位置——范围 [w, n-w)，即排除首尾各 w 根（旧 range(w, n-w) 语义）。

    向量化：sliding_window_view 一次构出全部长度为 w 的滑动窗，逐位取窗 max/min 与
    中心值比较。O(n) 内存 O(n×w) 计算，替代逐日窗口的 Python 级 O(n×w) 循环发射
    （P0-1 cProfile 实测 numpy reduce 叶子 1.455s 是 top-1 热点，即此循环的叶子开销）。

    NaN 语义：窗 max/min 用 numpy NaN 传播（与旧 values[i-w:i].min() 的 ndarray 口径
    一致——生产路径 detect 传的恒是 .values ndarray，NaN 邻居 → 比较 False → 不成极值）。
    短序列（n < 2w+1，旧 range 空循环）→ 全 False。

    注：旧 local_minima/local_maxima 函数保留（单测/外部脚本的行为锚），本掩码是
    fast path 的等价向量化版——两者一致性由 tests/test_p1_fast_path.py 直接对拍守护。
    """
    arr = values.values if hasattr(values, "values") else np.asarray(values)
    n = len(arr)
    mask = np.zeros(n, dtype=bool)
    if n < 2 * w + 1:
        return mask
    sw = sliding_window_view(arr, w)          # sw[j] = arr[j:j+w]，j ∈ [0, n-w]
    # 左窗 arr[i-w:i]（i ∈ [w, n-w)）→ sw[i-w]，j ∈ [0, n-2w)；右窗 arr[i+1:i+1+w]
    # → sw[i+1]，j ∈ [w+1, n-w]。max 用 >= / min 用 <= 与旧版严格对齐（并列极值都算）。
    if kind == "max":
        left = sw[:n - 2 * w].max(axis=1)
        right = sw[w + 1:n - w + 1].max(axis=1)
        ok = (arr[w:n - w] >= left) & (arr[w:n - w] >= right)
    else:
        left = sw[:n - 2 * w].min(axis=1)
        right = sw[w + 1:n - w + 1].min(axis=1)
        ok = (arr[w:n - w] <= left) & (arr[w:n - w] <= right)
    mask[w:n - w] = ok
    return mask


# ============================================================================
# 颈线搜索：顶部高点聚集定位 + 压制时长验证
# ============================================================================
def search_neckline(highs, closes, atr_val: float, min_touches: int, min_supp: float,
                    top_window: int = 3, decay_tau: float | None = None):
    """颈线 = 【顶部高点聚集】的价位（时间衰减加权定位）+ 【压制时长】验证。

    两步，角色严格分离：
      ① 定位（颈线在哪）：取窗口内【顶部高点（局部极大值）】，找它们聚集在哪个
         价位——即 ±ATR 带内含最多顶部高点的那个价位 c*。"顶点连成颈线"的本意。
      ② 验证（确认有效）：压制时长 = close<c* 的比例 ≥ min_supp。
         价格长期在颈线下方 = 阻力真实。

    为何不用"压制时长最大化"选位（旧版 bug）：
        c 越高 → close<c 越多 → 压制时长越大 → 选到窗口最高价附近，脱离真实阻力。
        压制时长只能当【验证】，不能当【选位标准】。选位必须用顶部聚集。

    时间衰减加权（2026-07-19 方案A，修颈线漂移 bug）：
        旧版等权聚集 Σ[顶部在±ATR带内]，固定窗口里旧高点（如 10-28 反弹顶，套牢盘
        已割肉=失效阻力）和近期高点（套牢盘还在=有效阻力）等权，旧高点污染颈线 +
        窗口滚动时颈线漂移（旧高点移出聚集中心，颈线下台阶"配合"假突破）。
        改 exp(−Δt/τ) 加权：近期顶部权重高（套牢盘还在），旧顶部淡出（套牢盘割肉）。
        Δt = 顶部到窗口末根（=当日）的天数，τ = 衰减常数（默认 30 日，主力近期记忆窗口）。
        聚集选位用加权 score，但要求等权 touches ≥ min_touches（聚集足够性，防衰减后
        单个近期顶部独占颈线）。decay_tau=None 退化为等权（兼容旧行为）。

    返回：(颈线价位 c, 压制时长 suppression)；无满足者返 (None, 0.0)。
    """
    # P1（2026-08-13）：旧版 inline tops 检测 + O(tops²) 双循环已下沉向量化内核
    # _neckline_cluster（与旧实现逐位等价，tests/test_p1_fast_path.py 随机扫场对拍守护）。
    # 本函数保留公开签名与语义契约（识别单源注释链），只做「掩码预计算 → 内核」薄包装。
    high_arr = highs.values if hasattr(highs, "values") else np.asarray(highs)
    close_arr = closes.values if hasattr(closes, "values") else np.asarray(closes)
    if len(high_arr) == 0:
        return None, 0.0
    tops_mask = local_extrema_mask(high_arr, top_window, kind="max")
    return _neckline_cluster(high_arr, close_arr, atr_val, min_touches, min_supp,
                             tops_mask, decay_tau)


def _neckline_cluster(highs_w, closes_w, atr_val, min_touches, min_supp,
                      tops_mask, decay_tau, decay_weights=None):
    """颈线聚集定位 + 压制验证的向量化内核（search_neckline 与 _detect_core_window 共用）。

    P1（2026-08-13 · spec §2.1）：替代旧 search_neckline 的 O(tops²) Python 双循环——
    tops×tops 外层差布尔矩阵 `D = |vals[:,None] - vals[None,:]| <= atr`，带内计数
    touches = D.sum(axis=1)，加权 score = D @ w_t（等权 w_t=1；衰减 w_t=exp(-Δt/τ)，
    Δt = 窗口末根(n-1) − 顶部窗口相对位置）。与旧实现逐位等价：

      - 首最大语义：旧 `if score > best_score and touches_eq >= min_touches` 顺序迭代
        严格更新 → 平局取位置靠前；向量化 `np.argmax(where(valid, scores, -inf))`
        对无效候选置 -inf、argmax 取首个最大——两者一致。
      - 压制时长：close<c* 的（衰减加权）比例 ≥ min_supp；等权退化为布尔计数/n。
      - 入参 tops_mask 为窗口相对布尔掩码（local_extrema_mask 产物，含 [w, n-w) 边界
        排除语义）；调用方（detect wrapper / scan_symbol fast loop）保证掩码与窗口对齐。

    返回 (颈线价位 c_star, 压制 suppression)；无满足者返 (None, 0.0)。
    """
    n = len(highs_w)
    tops_pos = np.flatnonzero(tops_mask)
    if len(tops_pos) < min_touches:
        return None, 0.0   # 顶部不够，连不成颈线
    tops_vals = highs_w[tops_pos]
    # 聚集布尔矩阵（对称）：带内 |t_i − t_j| <= atr
    D = np.abs(tops_vals[:, None] - tops_vals[None, :]) <= atr_val
    touches = D.sum(axis=1)
    use_decay = bool(decay_tau and decay_tau > 0)
    if use_decay:
        if decay_weights is None:
            # 衰减权重（窗口相对索引 i → exp(-((n-1)-i)/tau)，与旧 math.exp 逐位同口径）
            decay_weights = np.exp(-(np.arange(n - 1, -1, -1)) / decay_tau)
        w_t = decay_weights[tops_pos]
    else:
        w_t = np.ones(len(tops_pos), dtype=np.float64)
    scores = D @ w_t
    valid = touches >= min_touches   # 等权 touches 是聚集足够性阈值（衰减不豁免）
    if not valid.any():
        return None, 0.0   # 无满足聚集足够性的价位
    best = int(np.argmax(np.where(valid, scores, -np.inf)))   # 首个最大（对齐严格 > 更新）
    c_star = float(tops_vals[best])

    if use_decay:
        weights = (decay_weights if decay_weights is not None
                   else np.exp(-(np.arange(n - 1, -1, -1)) / decay_tau))
        sup_num = float(weights[closes_w < c_star].sum())
        suppression = float(sup_num / weights.sum())
    else:
        suppression = float((closes_w < c_star).sum() / n)
    if suppression < min_supp:
        return None, 0.0   # 压制时长不足，颈线无效
    return c_star, suppression


# ============================================================================
# 颈线法识别器主流程
# ============================================================================
def _detect_core_window(highs_w, lows_w, closes_w, vols_w, index_w, atr_val, cfg,
                        tops_mask_w=None, lows_mask_w=None, decay_weights=None):
    """detect 全守卫的数组内核（P1 · spec §2.1）——窗口切片视图 + 窗口相对掩码。

    与 detect_neckline_method 旧逐行逻辑逐位等价（三层守护：tests/test_p1_fast_path.py
    内核对拍 + tests/test_neckline_recognition.py 7 守卫 + P0-3 冻结基线 compare()）。
    入参均为「截至识别日的窗口」数组（长度 = cfg["window"]），掩码为窗口相对布尔掩码
    （local_extrema_mask 产物，含 [w, n-w) 边界排除语义）；None 则现场算（单次调用
    路径）。decay_weights 为 len=window 的衰减权重 exp(-((n-1)-i)/tau)，None 且
    decay_tau>0 时现场算（滚动扫描预计算复用，省每 T 重算 exp）。

    守卫顺序与旧版严格一致：ATR → 颈线（聚集+压制）→ 底部 → 突破 → 带量 → 深度 →
    盈亏比。任一不过返 None。
    """
    n = len(highs_w)
    if pd.isna(atr_val) or atr_val <= 0:
        return None

    if tops_mask_w is None:
        tops_mask_w = local_extrema_mask(highs_w, 3, kind="max")
    tau = cfg.get("decay_tau")
    if tau and tau > 0 and decay_weights is None:
        decay_weights = np.exp(-(np.arange(n - 1, -1, -1)) / tau)

    # —— 1. 颈线搜索（顶部聚集定位 + 压制时长验证，时间衰减加权）——
    c_star, suppression = _neckline_cluster(
        highs_w, closes_w, atr_val, cfg["min_touches"], cfg["min_suppression"],
        tops_mask_w, tau, decay_weights)
    if c_star is None:
        return None  # 无有效颈线（顶部不足 或 压制时长不足）

    # —— 2. 底部（最低点 + 带内离散低点）——
    # 旧版 pandas lows.min() 是 skipna；数组侧 np.nanmin 同语义（生产 OHLCV 无 NaN，
    # 语义仍逐位对齐——等价陷阱清单第 3 条）。
    min_price = float(np.nanmin(lows_w))
    if lows_mask_w is None:
        lows_mask_w = local_extrema_mask(lows_w, cfg["local_extrema_window"], kind="min")
    lows_pos = np.flatnonzero(lows_mask_w)
    lvals = lows_w[lows_pos]
    lvals = lvals[(lvals >= min_price) & (lvals <= min_price + atr_val)]
    bottom_set = {round(min_price, 4)}
    bottom_set.update(round(float(b), 4) for b in lvals)
    if len(bottom_set) < cfg["min_bottoms"]:
        return None  # 不足双底

    # —— 3. 突破（收盘越过颈线 + 带量）——
    close_T = float(closes_w[-1])
    if close_T <= c_star:
        return None  # 未突破颈线
    vol_T = float(vols_w[-1])
    # 旧版 pandas tail(5).mean() 是 skipna；数组侧 np.nanmean 同语义
    vol5 = float(np.nanmean(vols_w[-5:]))
    if vol5 > 0 and vol_T < cfg["breakout_vol_mult"] * vol5:
        return None  # 突破未带量

    # —— 4. 交易要素（颈线 + 最低点 → 进场/止损/止盈/rr）——
    # 进场 = 颈线价 c*（挂单等回踩；close>c* 只触发信号，不追涨）。
    # 止盈用 H 几何标尺（形态目标位：颈线+1H 第一波、颈线+N×H 第二波），
    # N=cfg["tp_h_mult"] 参数化（默认 2.0，对齐 caisen plan.neckline_height_multiple=2）。
    entry = c_star
    H = c_star - min_price                          # 形态几何深度（tp 定位标尺）
    if H <= 0:
        return None
    # ⑦ 形态深度过滤：H/ATR > max_h_atr 视为"暴跌反弹"（深形态全市场实证胜率仅 27%）
    h_over_atr = H / atr_val
    if h_over_atr > cfg.get("max_h_atr", 4.0):
        return None
    take_profit_1 = c_star + H                      # 第一波满足（几何，颈线+1H）
    take_profit_2 = c_star + cfg["tp_h_mult"] * H   # 第二波满足（几何，颈线+N×H）
    # —— R3 实际止损 + 实际盈亏比（2026-07-27 Task 1）——
    # Why：旧版 rr=2H/H=2.0 是几何 sanity，止损用谷底（min_price）跟执行层 simulate_exit
    # 的实际止损（颈线−stop_atr_mult×ATR，base_stop）口径脱节——detect 说"风险=H"，
    # 执行层真止损在颈线−N×ATR（往往远高于谷底），min_rr 没把住真实风险收益。
    # 修正：detect 显式产出 stop_price（与执行层同口径），rr 用实际盈亏比
    # (tp2−entry)/(entry−stop_price)，min_rr 验真实盈亏比。
    # 止盈仍用 H 几何标尺（形态目标位，与 caisen plan 对齐），止损改 ATR 波动标尺（风控口径）。
    stop_price = c_star - cfg["stop_atr_mult"] * atr_val   # 实际止损（执行层 base_stop 同口径）
    risk_dist = entry - stop_price
    if risk_dist <= 0:
        return None  # 防御：颈线 ≤ stop_price（ATR 异常放大），无风险距离无意义
    rr = (take_profit_2 - entry) / risk_dist              # 实际口径盈亏比（替换旧 2H/H 几何）
    if rr < cfg["min_rr"]:
        return None

    return {
        "formed_at": index_w[-1],
        "neckline": round(c_star, 3),
        "suppression": round(suppression, 3),
        "bottom": round(min_price, 3),
        "n_bottoms": len(bottom_set),
        "entry": round(entry, 3),
        "stop": round(min_price, 3),                # 谷底（保留，H 计算基准 + 形态参考）
        "stop_price": round(stop_price, 3),         # R3 实际交易止损（颈线−N×ATR，执行层 base_stop 同口径）
        "take_profit_1": round(take_profit_1, 3),
        "take_profit_2": round(take_profit_2, 3),
        "H": round(H, 3),
        "H_over_ATR": round(h_over_atr, 2),          # 形态深度（实证关键分水岭）
        "rr": round(rr, 3),                          # R3 实际口径盈亏比（替换旧几何 2H/H）
        "atr": round(atr_val, 3),
    }


def detect_neckline_method(df: pd.DataFrame, cfg: dict = DEFAULTS, atr_series=None):
    """对单标的 OHLCV 时序执行颈线法识别，返回候选 dict 或 None。

    atr_series: 可选预算 ATR 序列（回测滚动复用，避免每 T 重算 compute_atr）；
                None 则内部现算。

    P1（2026-08-13）：7 守卫逻辑已下沉数组内核 _detect_core_window，本函数是公开签名的
    薄包装（df 路径：实盘 scan_live / 回测 scan_at / 测试调用）——tail(window) 提取数组
    → 现场算极值掩码 → 调内核。研究侧 scan_symbol 走 detect_signal_fast（同一内核，
    全序列预计算掩码复用）——识别单源（内核一份，两侧共享）。
    """
    window = cfg["window"]
    if len(df) < window:
        return None

    W = df.tail(window)

    # ATR：外部传 atr_series（回测预算复用）则用末根；否则内部全序列算。
    # 窗口对齐 cfg["window"]（颈线识别窗口），尺度统一——形态在 window 天形成，
    # 衡量其波动尺度也应用 window 天，而非写死的 14 天短期 ATR。
    if atr_series is not None:
        atr_val = float(atr_series.iloc[-1])
    else:
        atr_val = float(compute_atr(df["high"], df["low"], df["close"], window=cfg["window"]).iloc[-1])
    if pd.isna(atr_val) or atr_val <= 0:
        return None

    highs = W["high"].values
    lows_w = W["low"].values
    closes_w = W["close"].values
    vols_w = W["volume"].values
    # 极值掩码（现场算：单次调用路径；滚动扫描走 detect_signal_fast 全序列预计算复用）
    tops_mask = local_extrema_mask(highs, 3, kind="max")
    lows_mask = local_extrema_mask(lows_w, cfg["local_extrema_window"], kind="min")
    return _detect_core_window(highs, lows_w, closes_w, vols_w, W.index, atr_val, cfg,
                               tops_mask, lows_mask)


# ============================================================================
# detect_signal：识别纯函数（Task 2 · U2 识别统一）
# ============================================================================
# 物理定位：
#     从 NecklineMethodStrategy.scan_live（strategies/neckline_method.py:218-298）
#     抽取的【纯识别函数】——ATR 全序列预算 → detect_neckline_method（含 R2 窗口已突破）
#     → R1 cancel_on close 口径预判 → 当日突破过滤 → 装配完整 Signal。
#
#     本函数是【已测但未挂接】的纯函数（Task 2 范围）——scan_live/scan_at/scan_symbol
#     的调用点改接在 Task 3 做（strangler 红线：从 scan_live 逻辑零改动抽取，不顺手优化）。
#
# 等价性红线（与 scan_live:218-299 逐位一致）：
#     - ATR 全序列预算用 id_cfg["window"]（颈线识别窗口，非写死 14）—— line 221-223
#     - detect_neckline_method(df_upto, id_cfg, atr_series=atr_full) —— line 228
#     - cancel_on 守卫用【close】（已是 close 口径，D9 实盘侧就位）—— line 252-258
#     - 当日突破过滤（formed_at == date，两侧 ISO 日期字符串归一）—— line 264-273
#     - Signal 装配（entry=颈线+buy_limit_mult×ATR, atr=ATR末值, rr=res["rr"]）—— line 279-299
def detect_signal(symbol, df_upto, id_cfg, exec_cfg, date, atr_full=None):
    """颈线法识别纯函数：从截至 date 的 df_upto 产出完整 Signal 或 None。

    逻辑零改动抽取自 NecklineMethodStrategy.scan_live:218-298（strangler 红线）。
    与 scan_live 的唯一差异：scan_live 返 ``list[Signal]``（实盘多信号容器约定），
    本函数返 ``Signal | None``（纯识别单信号，调用方 Task 3 按入口语义包装成 list/None）。

    无前视契约：
        df_upto 由调用方（_eod / scan_at）从 data_lake 加载该 symbol 截至 date 的前复权
        日线（截断于 date，不含 date 之后），atr 也在 df_upto 上算——严格因果。

    参数：
        symbol: 标的代码（Signal 归因用，如 "600000.SH"）
        df_upto: 该 symbol 截至 date 的前复权日线 DataFrame（OHLCV，DatetimeIndex）
        id_cfg: 识别层参数 dict（window/min_touches/...，与 DEFAULTS 同键集）
        exec_cfg: 执行层参数 dict（buy_limit_atr_mult/cancel_thresh_mult/...，
                  与 EXEC_DEFAULTS 同键集）
        date: 当前识别日（_eod 传 T-1 收盘日，str 或 pd.Timestamp 均可）
        atr_full: 可选预计算 ATR 全序列（窗口对齐 id_cfg["window"]，index 与 df_upto
            对齐；滚动扫描调用方预算一次按 T 截断传入，省每 T 全量重算，P1-6）。
            None → 内部自算（实盘/单次调用零改动，向后兼容）。

    返回：
        Signal（含 symbol/formed_at/breakout_date/neckline/bottom/entry_price/atr/rr）
        或 None（detect 无命中 / cancel_on close 守卫触发 / 非当日突破）。
    """
    # ATR 预算（窗口对齐 id_cfg["window"]，与 scan_at / precompute 同口径）。
    # 物理意图：颈线在 window 天形成，衡量其波动尺度也用 window 天，而非写死 14 天。
    # 截至此处仅用 df_upto（无前视），末根即 date 当日的 ATR。
    # P1-6：调用方可传预计算 atr_full（滚动扫描预算一次按 T 截断）避免每 T 全量重算；
    # 传入序列须与 df_upto 同 index 前缀（截断至 date），末值即 date 当日 ATR。
    if atr_full is None:
        atr_full = compute_atr(
            df_upto["high"], df_upto["low"], df_upto["close"], window=id_cfg["window"]
        )

    # 识别：detect_neckline_method（df_upto 截至 date，atr_series 末根对齐）。
    # detect 仅在末根突破时返回（内部 close_T = W["close"].iloc[-1] > c_star 才命中），
    # 故 res["formed_at"] == df_upto.index[-1] == date（正常路径）。detect 内部已含
    # R2 窗口已突破判定（窗口内已突破形态返 None），detect_signal 直接透传 None。
    res = detect_neckline_method(df_upto, id_cfg, atr_series=atr_full)

    # R1 cancel_on 守卫 + 当日突破过滤 + Signal 装配已下沉 _post_detect（P1 · spec §2.1）——
    # 与 detect_signal_fast（研究侧数组路径）共用同一装配闭包，识别装配单源防分叉。
    close_T = float(df_upto["close"].iloc[-1])
    atr_last = atr_full.iloc[-1]
    return _post_detect(symbol, res, exec_cfg, date, close_T, atr_last)


def _post_detect(symbol, res, exec_cfg, date, close_T, atr_last):
    """detect 之后的 R1 cancel_on 守卫 + 当日突破过滤 + Signal 装配（识别装配单源）。

    P1（2026-08-13 · spec §2.1）：从 detect_signal 抽取的共用后半段——detect_signal
    （df 路径：实盘 scan_live / 回测 scan_at）与 detect_signal_fast（数组路径：研究侧
    scan_symbol）共用，防「研究侧 fast path 与实盘 df 路径装配分叉」。语义与原
    detect_signal 后半段逐位一致：
      - cancel_on 守卫（close 口径，D9）：close_T ≥ 颈线+cancel_thresh_mult×H → None
      - 当日突破过滤：formed_at 与 date 归一短 ISO 比较（C1 类型对齐 fix）
      - 装配：entry_price 回退 `(res.get("atr") or 0.0)`；atr 字段回退 `res.get("atr")`
        ——两处回退口径不同（带 or 0.0 / 不带），勿合并。
    """
    if res is None:
        return None

    # R1 cancel_on 预判（close 口径，D9 实盘侧就位）：挡缺口1+4。
    # What：把 execute 层 cancel_on 撤单逻辑前移为识别期预判——避免实盘挂上废单后再撤
    #       的滑点/费率/状态机污染。识别期用【当日 close】判（T-1 晚只有完整收盘 K 线，
    #       没有次日盘中 high 可用，无前视），execute 层 simulate_exit 用盘中 high 判
    #       （high≥cancel_on 摸高即撤）——close≥cancel_on 蕴含 high≥cancel_on，故
    #       close 守卫是 high 守卫的保守近似（识别期更严，execute 层不漏挡）。
    # 参数复用：exec_cfg["cancel_thresh_mult"]（默认 1.0=颈线+H），与 execute 层撤单
    #          阈值同口径——识别层挡多少、执行层就撤多少。
    # 物理标尺：H = 颈线 - 谷底（形态几何深度，detect 已返回 res["bottom"]）。
    cancel_thresh = exec_cfg.get("cancel_thresh_mult")
    if cancel_thresh is not None:
        H = res["neckline"] - res["bottom"]
        cancel_on = res["neckline"] + cancel_thresh * H
        if close_T >= cancel_on:
            return None  # 涨幅已兑现，不产回踩挂单信号（挡冲天突破）

    # 当日突破过滤（防御层）：只挂当日新信号。
    # Why：detect 物理上只在末根突破时返，此处等于 date 是常态；但显式校验防 detect
    # 内部窗口语义未来变化（如支持历史日回溯）时把旧信号当新信号重吐占仓。
    # 类型对齐（C1 final-fix）：detect 返 formed_at 是 pd.Timestamp，date 可能是 str
    # （_eod 真实调用约定 strftime 出来）。pandas __ne__ 不像 __eq__ 做字符串解析，
    # Timestamp != str 恒 True → 所有真实信号被误判为历史信号丢弃 → 实盘静默死亡。
    # 两侧统一用 pd.Timestamp(...).strftime("%Y-%m-%d") 归一为短 ISO 比较才在任意类型
    # 组合下都能正确做物理同日判定。
    breakout_date = res.get("formed_at")
    if pd.Timestamp(breakout_date).strftime("%Y-%m-%d") != pd.Timestamp(date).strftime("%Y-%m-%d"):
        return None

    # Signal dataclass（实盘纯识别字段集，不掺 simulate_exit 的出场字段）。
    # entry_price：颈线 + buy_limit_atr_mult × ATR末值（对齐回测 simulate_exit:75
    # buy_limit=c_star+buy_limit_atr_mult×atr）。mult=0 退化颈线（零回归）；
    # atr 缺失/NaN 回退 res["atr"]（与 scan_live:289-290 同口径）。
    # atr：用 atr_full 末值（对齐 date 当日，供二期引擎算止损=颈线−N×ATR 用）。
    atr_ok = float(atr_last) if not pd.isna(atr_last) else None
    return Signal(
        symbol=symbol,
        signal_type="neckline",
        formed_at=res.get("formed_at"),
        breakout_date=res.get("formed_at"),
        neckline=res.get("neckline"),
        bottom=res.get("bottom"),
        entry_price=(res.get("neckline") or 0.0) + exec_cfg.get("buy_limit_atr_mult", 1.0) * (
            atr_ok if atr_ok is not None else (res.get("atr") or 0.0)),
        atr=atr_ok if atr_ok is not None else res.get("atr"),
        # R3 实际口径盈亏比透传（detect 已算 (tp2-entry)/(entry-stop_price)，
        # 基于颈线-N×ATR 止损 / 颈线+N×H 止盈的真实风险报酬比），供研究员 T-1 晚人审。
        rr=res.get("rr"),
    )


def detect_signal_fast(symbol, arr, pos, id_cfg, exec_cfg, date, atr_arr,
                       tops_mask=None, lows_mask=None, decay_weights=None):
    """fast path 识别（P1 · spec §2.1）：全序列数组 + 截至 pos 的窗口识别。

    与 detect_signal(symbol, df.iloc[:pos+1], id_cfg, exec_cfg, date,
                     atr_full=atr.iloc[:pos+1]) 逐位等价——研究侧 scan_symbol 逐日滚动
    扫描改走本入口（预算好的 arr/极值掩码/ATR/衰减权重复用，窗口切片=零拷贝视图），
    消除每 T 的 sym_df.iloc[:i+1] + atr_full.iloc[:i+1] O(n²) DataFrame 拷贝
    （P0-1 实测识别路径占 scan_symbol cumtime ~80% 主导，本函数即对症改造）。

    参数：
        arr:  全序列数组上下文 {"high"/"low"/"close"/"volume": ndarray, "index": DatetimeIndex}
        pos:  截至位置（含，识别日 = arr["index"][pos]）
        atr_arr: 预算好的 ATR 全序列（compute_atr 产物 to_numpy()，窗口对齐 id_cfg["window"]）
        tops_mask/lows_mask: 全序列极值掩码（local_extrema_mask 产物，调用方预计算复用；
            窗口切片与「在窗口上现场算掩码」逐位一致——局部比较只用窗内邻居，P0 交接注记）
        decay_weights: len=window 衰减权重（调用方预计算；None 时内核现场算）
    其余参数语义同 detect_signal（date 归一 ISO 比较，cancel_on close 守卫共用 _post_detect）。
    等价性由 tests/test_p1_fast_path.py（df 路径 == 数组路径）+ P0-3 冻结基线守护。
    """
    window = id_cfg["window"]
    if pos + 1 < window:
        return None   # 数据不足窗口（与 detect len(df)<window 守卫同口径）
    atr_val = float(atr_arr[pos])
    if pd.isna(atr_val) or atr_val <= 0:
        return None   # ATR 无效（与 detect 的 atr 守卫同口径）
    s = pos - window + 1
    highs_w = arr["high"][s:pos + 1]
    lows_w = arr["low"][s:pos + 1]
    closes_w = arr["close"][s:pos + 1]
    vols_w = arr["volume"][s:pos + 1]
    index_w = arr["index"][s:pos + 1]
    # 掩码边界裁剪（等价红线关键）：全序列掩码的 True 位含窗口首尾各 w 根的「边界区」
    # ——这些位置的局部极值判定用了窗口外的邻居，与旧版 detect 的极值循环（range(w, n-w)
    # 只取窗口相对 [w, n-w)）口径不同。正确裁剪 = 取窗口切片后**零化首尾边界区**（保持
    # 窗口相对坐标不变；若用 [s+w:pos-w+1] 裁切片则 flatnonzero 产出的是切片相对位置，
    # 与窗口相对坐标差 w 偏移——P0-3 冻结基线对拍曾抓出两种口径的分叉）。零化后与
    # 「在窗口上现场算掩码」（df 路径 detect_neckline_method 的口径）逐位一致。
    # .copy() 必加：基本切片是视图，写零会污染调用方预计算的全序列掩码。
    tops_slice = None
    if tops_mask is not None:
        tops_slice = tops_mask[s:pos + 1].copy()
        tops_slice[:3] = False            # 窗口相对 [0,3) 边界排除（top_window=3）
        tops_slice[-3:] = False           # 窗口相对 [n-3, n) 边界排除
    w_ext = id_cfg["local_extrema_window"]
    lows_slice = None
    if lows_mask is not None:
        lows_slice = lows_mask[s:pos + 1].copy()
        lows_slice[:w_ext] = False        # 窗口相对 [0, w_ext) 边界排除
        lows_slice[-w_ext:] = False       # 窗口相对 [n-w_ext, n) 边界排除
    res = _detect_core_window(highs_w, lows_w, closes_w, vols_w, index_w, atr_val, id_cfg,
                              tops_slice, lows_slice, decay_weights)
    close_T = float(closes_w[-1])
    return _post_detect(symbol, res, exec_cfg, date, close_T, atr_val)


# ============================================================================
# 测试入口：单标的滚动 replay（每历史日重判，验证逻辑闭环）
# ============================================================================
def main():
    lake_path = "data_lake/a_shares_daily.parquet"
    if not os.path.exists(lake_path):
        print(f"[ERROR] 数据湖缺失：{lake_path}")
        return
    print(f"加载 {lake_path} ...")
    lake = pd.read_parquet(lake_path)

    symbol = "000001.SZ"
    try:
        sym_df = lake.xs(symbol, level="symbol").sort_index()
    except KeyError:
        print(f"[ERROR] 标的 {symbol} 不在湖中")
        return

    window = DEFAULTS["window"]
    print(f"标的={symbol}，总K线={len(sym_df)}，窗口={window}")
    print(f"参数：{DEFAULTS}\n")

    hits = []
    for i in range(window, len(sym_df)):
        sub = sym_df.iloc[: i + 1]
        res = detect_neckline_method(sub, DEFAULTS)
        if res is not None:
            res["symbol"] = symbol
            hits.append(res)

    print(f"=== 识别到 {len(hits)} 个颈线法形态 ===\n")
    for h in hits[-15:]:
        print(
            f"{h['formed_at'].date()} | 颈线={h['neckline']:<8} "
            f"压制={h['suppression']:<5} 底={h['bottom']:<8} "
            f"{h['n_bottoms']}底 | 进={h['entry']:<8} 止损={h['stop']:<8} "
            f"止盈2={h['take_profit_2']:<8} rr={h['rr']}"
        )

    if hits:
        print(f"\n[样例详情] 最近一个命中：")
        for k, v in hits[-1].items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
