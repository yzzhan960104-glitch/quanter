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
    ⑥ 盈亏比 = 2H/H = 2.0 结构恒定（H=颈线−谷底），min_rr 作 sanity 守卫

交易要素（用户规则，持有期模拟见 neckline_backtest.py）：
    进场执行 = T+1 日收盘买入；止损 = 颈线 c*；止盈 = 50%@颈线+H，50%@颈线+2H；
    超时 = 15 日未达止盈收盘卖剩余。

风控边界（CLAUDE.md 极简 + 显式 + 防御性）：
    - 数据不足（< 窗口）/ ATR 无效 / 颈线或谷底异常 → 显式返 None；
    - 局部极值用左右各 w 根比较，排除窗口边界 w 根；
    - 窗口最低点强制纳入底部集合（anchor）。

用法：
    PYTHONIOENCODING=utf-8 python -u scripts/neckline_method_v0.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

# 加项目根到 sys.path（脚本可从任意 cwd 运行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
    "min_rr": 1.5,             # ⑥ 盈亏比下限（恒 2.0，作 sanity 守卫）
    "max_h_atr": 4.0,          # ⑦ 形态深度上限 H/ATR（实证：浅形态胜率51% vs 深形态27%，深=暴跌反弹）
}


# ============================================================================
# 基元：ATR（与 caisen.patterns.zigzag_causal.compute_atr 同口径，自写避免依赖）
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


# ============================================================================
# 颈线搜索：顶部高点聚集定位 + 压制时长验证
# ============================================================================
def search_neckline(highs, closes, atr_val: float, min_touches: int, min_supp: float,
                    top_window: int = 3):
    """颈线 = 【顶部高点聚集】的价位（定位）+ 【压制时长】验证（验证）。

    两步，角色严格分离：
      ① 定位（颈线在哪）：取窗口内【顶部高点（局部极大值）】，找它们聚集在哪个
         价位——即 ±ATR 带内含最多顶部高点的那个价位 c*。"顶点连成颈线"的本意。
      ② 验证（确认有效）：压制时长 = close<c* 的比例 ≥ min_supp。
         价格长期在颈线下方 = 阻力真实。

    为何不用"压制时长最大化"选位（旧版 bug）：
        c 越高 → close<c 越多 → 压制时长越大 → 选到窗口最高价附近，脱离真实阻力。
        压制时长只能当【验证】，不能当【选位标准】。选位必须用顶部聚集。

    返回：(颈线价位 c, 压制时长 suppression)；无满足者返 (None, 0.0)。
    """
    n = len(highs)
    if n == 0:
        return None, 0.0

    # ① 定位：顶部高点聚集
    tops = local_maxima(highs, top_window)
    if len(tops) < min_touches:
        return None, 0.0  # 顶部不够，连不成颈线
    best_c, best_touches = None, 0
    for c in tops:
        # 该价位 ±ATR 带内的顶部高点数（聚集程度）
        touches = sum(1 for t in tops if abs(t - c) <= atr_val)
        if touches > best_touches:
            best_c, best_touches = float(c), touches
    if best_touches < min_touches or best_c is None:
        return None, 0.0  # 聚集不够，无颈线

    # ② 验证：压制时长
    suppression = float((closes < best_c).sum() / n)
    if suppression < min_supp:
        return None, 0.0  # 压制时长不足，颈线无效
    return best_c, suppression


# ============================================================================
# 颈线法识别器主流程
# ============================================================================
def detect_neckline_method(df: pd.DataFrame, cfg: dict = DEFAULTS, atr_series=None):
    """对单标的 OHLCV 时序执行颈线法识别，返回候选 dict 或 None。

    atr_series: 可选预算 ATR 序列（回测滚动复用，避免每 T 重算 compute_atr）；
                None 则内部现算。
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
    closes_w = W["close"].values
    lows = W["low"]

    # —— 1. 颈线搜索（顶部聚集定位 + 压制时长验证）——
    c_star, suppression = search_neckline(
        highs, closes_w, atr_val, cfg["min_touches"], cfg["min_suppression"]
    )
    if c_star is None:
        return None  # 无有效颈线（顶部不足 或 压制时长不足）

    # —— 2. 底部（最低点 + 带内离散低点）——
    min_price = float(lows.min())
    local_lows = local_minima(lows.values, cfg["local_extrema_window"])
    bottoms = [l for l in local_lows if min_price <= l <= min_price + atr_val]
    bottom_set = {round(min_price, 4)}
    bottom_set.update(round(b, 4) for b in bottoms)
    if len(bottom_set) < cfg["min_bottoms"]:
        return None  # 不足双底

    # —— 3. 突破（收盘越过颈线 + 带量）——
    close_T = float(W["close"].iloc[-1])
    if close_T <= c_star:
        return None  # 未突破颈线
    vol_T = float(W["volume"].iloc[-1])
    vol5 = float(W["volume"].tail(5).mean())
    if vol5 > 0 and vol_T < cfg["breakout_vol_mult"] * vol5:
        return None  # 突破未带量

    # —— 4. 交易要素（颈线 + 最低点 → 进场/止损/止盈/rr）——
    # 进场 = 颈线价 c*（挂单等回踩；close>c* 只触发信号，不追涨）。
    # 使 risk=H、rr=2H/H=2.0 结构恒定，盈亏比由形态结构决定，不依赖突破日涨幅。
    # 止盈用第二波满足（颈线+2H），对齐 caisen plan.neckline_height_multiple=2。
    entry = c_star
    stop = min_price                         # 谷底（注：持有期模拟里止损改为颈线，见 backtest）
    H = c_star - min_price
    if H <= 0:
        return None
    # ⑦ 形态深度过滤：H/ATR > max_h_atr 视为"暴跌反弹"（深形态全市场实证胜率仅 27%）
    h_over_atr = H / atr_val
    if h_over_atr > cfg.get("max_h_atr", 4.0):
        return None
    take_profit_1 = c_star + H               # 第一波满足（50% 止盈位）
    take_profit_2 = c_star + 2 * H           # 第二波满足（50% 止盈位）
    rr = (take_profit_2 - entry) / H         # = 2H/H = 2.0
    if rr < cfg["min_rr"]:
        return None

    return {
        "formed_at": W.index[-1],
        "neckline": round(c_star, 3),
        "suppression": round(suppression, 3),
        "bottom": round(min_price, 3),
        "n_bottoms": len(bottom_set),
        "entry": round(entry, 3),
        "stop": round(stop, 3),
        "take_profit_1": round(take_profit_1, 3),
        "take_profit_2": round(take_profit_2, 3),
        "H": round(H, 3),
        "H_over_ATR": round(h_over_atr, 2),   # 形态深度（实证关键分水岭）
        "rr": round(rr, 3),
        "atr": round(atr_val, 3),
    }


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
