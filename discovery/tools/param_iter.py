# -*- coding: utf-8 -*-
"""颈线法参数空间定义（PARAM_SPACE）——discovery 搜索层与 sampler 的共享配置源。

⚠️ 已退役说明（2026-08-05 SSoT review-fix2，B3 收口彻底落地）：
    legacy 参数搜索入口（main / load_state / save_state / STATE_FILE）已**整块删除**。
    参数搜索的单一真相源是 ``discovery daemon``（L0-L5：快照冻结 → holdout →
    Sobol/TPE → Pareto/DSR → 跨夜收敛 → publish 到 experiment DRAFT）；继续保留 legacy
    入口会产生双轨冠军（logs/param_iter_state.json vs experiment.db ACTIVE），
    且其 kelly/freq_cap 口径高估年化（如 115.8% ann / 0.9% dd），误导播报与周度回测。

    本模块当前职责**仅剩 PARAM_SPACE 配置**（21 维候选档，分层标记 id/exec）：
      · ``discovery.sampler`` 通过 ``from discovery.tools.param_iter import PARAM_SPACE``
        复用同源候选档（避免重造 21 维空间定义）；
      · 其它 legacy 函数（score_of / run_one / load_universe / random_params /
        neighbor_params / params_key / is_target_board / _breadth_at）保留为
        内核数值回归测试与 discovery 子模块（snapshot/objective）的「同口径锚」参考
        ——它们**不被生产链路调用**，仅作为参照实现存在（删除会破坏 golden 回归基线
        与 discovery snapshot 的同源契约注释链，且无 SSoT 风险——零写入零状态）。

历史重构（2026-07-20，详见 memory caisen-neckline-paramiter-baseline）：
  ① universe 收窄：创业板(300/301)+科创板(688/689)，2025-01-01 至今，可交易(≥1亿) ≈1334 只
  ② 22 维概念全调：识别层 11（DEFAULTS）+ 执行层 10（EXEC_DEFAULTS 7 + trailing 3）= 21 可调
                   + universe 固定 = 22 概念
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 脚本直跑时 sys.path[0] 是脚本目录而非仓库根，`from strategies...` 会 ModuleNotFoundError。
# 补插仓库根（tools → discovery → 仓库根 三级上溯），与 qmt_smoke.py 同款范式。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from strategies.neckline.method_v0 import DEFAULTS
from strategies.neckline.backtest import scan_symbol, risk_metrics, EXEC_DEFAULTS


# ===== 21 维参数空间（识别层 id=DEFAULTS / 执行层 exec=EXEC_DEFAULTS）=====
# 每维给 2-3 档候选；window/min_suppression/max_h_atr/stop_atr_mult/tp_h_mult 沿用 v1 网格档
# 新纳入：min_touches/local_extrema_window/min_bottoms/breakout_vol_mult/min_rr/decay_tau（识别层原漏调）
#         + max_holding/max_wait/cooldown/buy_limit_atr_mult/tp1_h_mult/tp1_portion/cancel_thresh_mult（执行层原硬编码）
PARAM_SPACE = [
    # —— 识别层（DEFAULTS）：形态判定 ——
    ("window",              "id",   [40, 60, 80]),          # ① 识别窗口
    ("min_touches",         "id",   [2, 3]),                # ② 颈线聚集足够性
    ("min_suppression",     "id",   [0.5, 0.6, 0.7]),       # ③ 压制时长
    ("local_extrema_window","id",   [3, 5]),                # ④ 底部极值窗口
    ("min_bottoms",         "id",   [2, 3]),                # ⑤ 双底/三底门槛
    ("breakout_vol_mult",   "id",   [1.0, 1.5, 2.0]),       # ⑥ 突破带量
    ("min_rr",              "id",   [1.0, 1.5, 2.0]),       # ⑦ 盈亏比守卫
    ("max_h_atr",           "id",   [3.0, 4.0, 5.0]),       # ⑧ 形态深度上限
    ("stop_atr_mult",       "id",   [1.0, 1.5]),            # ⑨ 止损 ATR 倍数
    ("tp_h_mult",           "id",   [1.5, 2.0, 2.5]),       # ⑩ 止盈2 的 H 倍数
    ("decay_tau",           "id",   [None, 30, 60]),        # ⑪ 颈线时间衰减（None=等权）
    # —— 执行层（EXEC_DEFAULTS）：挂单/止盈/仓位/撤单 ——
    ("max_holding",         "exec", [10, 15, 20]),          # ⑫ 成交后超时持仓日
    ("max_wait",            "exec", [3, 5, 8]),             # ⑬ 挂单等回踩有效期
    ("cooldown",            "exec", [3, 5, 8]),             # ⑭ 信号去重冷却
    ("buy_limit_atr_mult",  "exec", [0.5, 1.0, 1.5]),       # ⑮ 挂单价 ATR 倍数
    ("tp1_h_mult",          "exec", [0.5, 1.0, 1.5]),       # ⑯ 止盈1 的 H 倍数
    ("tp1_portion",         "exec", [0.3, 0.5, 0.7]),       # ⑰ 止盈1 减仓比例
    ("cancel_thresh_mult",  "exec", [None, 1.0, 2.0]),      # ⑱ 撤单阈值（None=不撤放飞）
    # —— trailing 时间驱动移动止损（海龟风格 · simulate_exit 生效条件 grace>0 AND step>0）——
    # grace=0 退化为固定止损（=当前 EXEC_DEFAULTS 默认，作基线对照，验证 trailing vs 固定谁优）
    ("trailing_grace",  "exec", [0, 5, 10]),        # ⑲ 宽限期（0=关闭固定止损；5/10=前 N 天不收紧）
    ("trailing_step",   "exec", [0.05, 0.1, 0.15]), # ⑳ 收紧速度（ATR/日；grace 后每日 stop 上移）
    ("trailing_floor",  "exec", [0.0, 0.5]),        # ㉑ 收紧下限（0=到颈线；0.5=颈线−0.5ATR 卡住）
]
# universe（创板+科创 2025至今）固定不调 = 第 22 个"概念参数"（21 可调 + universe）

# 目标年化与回测起始日（保留供内核回归测试 / discovery snapshot 同源契约参照）。
# legacy STATE_FILE / load_state / save_state / main 已于 2026-08-05（SSoT review-fix2）
# 整块删除——参数搜索单一真相源是 discovery daemon + experiment.db ACTIVE。
TARGET_ANN = 0.90          # 目标凯利年化 90%（用户 2026-07-21：≥90% 同时高夏普低回撤）
START_DATE = "2025-01-01"  # 回测起始日（缩短年限提速）


def score_of(ann, sharpe, max_dd):
    """复合软约束：ann × 夏普 / (1+回撤)，全程有梯度（ann<90% 不归零）。

    物理意图（用户 2026-07-21 复核改版：约束式 90% 硬门槛实证 5 组全 0、邻域退化 → 改软约束）：
    ann 在乘积里天然主导（高 ann 分高，邻域向高 ann 爬），夏普/回撤作风险调整。
    达标区(ann≥90%)与非达标区连续过渡——不强制 90% 但 ann 大本身让分高，自动接近
    「≥90% + 高夏普 + 低回撤」诉求。ann≤0（亏钱）或 max_dd≥100%（爆仓）归零。
    例：ann79%×夏普9/(1+3%)≈6.9；ann95%×夏普9/(1+3%)≈8.3（达标组分更高）。
    """
    if ann <= 0 or max_dd >= 1.0:
        return 0.0
    return ann * sharpe / (1.0 + max_dd)


def _breadth_at(breadth, date):
    """查 market_breadth 在 date 的值（date 非交易日 → ffill 最近≤date 的值，无前视）。

    breadth: pd.Series（DatetimeIndex 逐日，值=站上 MA60 比例）。date: date/Timestamp。
    返回 float 或 None（无数据）。P1-c 宽度顺势加权用。
    """
    try:
        ts = pd.Timestamp(date)
        if ts in breadth.index:
            return float(breadth.loc[ts])
        idx = breadth.index.get_indexer([ts], method="ffill")[0]
        return float(breadth.iloc[idx]) if idx >= 0 else None
    except Exception:
        return None


def is_target_board(sym):
    """创业板(300/301) + 科创板(688/689)。"""
    code = sym.split(".")[0]
    return code.startswith(("300", "301", "688", "689"))


def load_universe():
    """加载创板+科创 2025至今可交易(近30日均成交额≥1亿)标的 → {symbol: sym_df}。"""
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
    lake = lake[lake.index.get_level_values("date") >= pd.Timestamp(START_DATE)]
    syms = lake.index.get_level_values("symbol").unique().tolist()
    amt = lake.groupby("symbol")["amount"].apply(lambda s: s.tail(30).mean() if len(s) > 0 else 0.0)
    tradable = [s for s in syms if is_target_board(s) and amt.get(s, 0.0) >= 1e5]
    universe = {}
    for s in tradable:
        try:
            universe[s] = lake.xs(s, level="symbol").sort_index()
        except Exception:
            continue
    return universe


def random_params(rng):
    """阶段1：每维独立随机选一个候选值（覆盖 18 维空间）。"""
    return {k: rng.choice(v) for k, _, v in PARAM_SPACE}


def neighbor_params(params, rng):
    """阶段2：贪心邻域——从基准参数随机选 1-2 维移到相邻档（±1）。"""
    nb = dict(params)
    keys = rng.sample(list(params.keys()), rng.choice([1, 1, 2]))  # 2/3 概率改1维，1/3 改2维
    for k in keys:
        vals = next(v for kk, _lay, v in PARAM_SPACE if kk == k)
        cur = params[k]
        idx = vals.index(cur) if cur in vals else len(vals) // 2
        new_idx = max(0, min(len(vals) - 1, idx + rng.choice([-1, 1])))
        nb[k] = vals[new_idx]
    return nb


def params_key(params):
    """参数组 → 去重用的稳定字符串键（None 也要能序列化）。"""
    return json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)


def run_one(params, universe, breadth=None):
    """跑一组参数：显式构造 id_cfg/exec_cfg 传入 scan_symbol，遍历 universe 算凯利年化。

    breadth: 可选 market_breadth Series（P1-c 宽度顺势加权）。非空时信号日 breadth≥0.4
        → avg_pnl×1.5（等效加仓 1.5×，研究验证 memory 2024 +1.95→+5.43%；research 用途，
        筹效突破 pos_cap，实盘需另调 cap 或加权方式）。
    返回 (年化, 凯利, 曲线, 笔数, 夏普, 回撤)。

    重构（2026-07-23 Task 5 #2a）：去全局 mutation（原 DEFAULTS.update/EXEC_DEFAULTS.update
    + try/finally 恢复），改显式构造 id_cfg/exec_cfg 传入 scan_symbol。scan_symbol 的
    id_cfg 默认 {**DEFAULTS, window:window} 会拷贝全局——显式传则绕过全局，与原 update
    全局后 scan_symbol 拷贝全局的行为逐字等价（detect_neckline_method 只读传入 cfg，
    不读全局 DEFAULTS）。零全局可变状态 → 单/多进程皆安全，无需恢复。
    """
    id_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "id"}
    exec_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "exec"}
    # 显式构造识别层/执行层 cfg 传入 scan_symbol（不再 mutation 全局 DEFAULTS/EXEC_DEFAULTS）。
    id_cfg = {**DEFAULTS, **id_params}
    exec_cfg = {**EXEC_DEFAULTS, **exec_params}
    window = id_cfg["window"]
    all_filled = []
    for sym, sym_df in universe.items():
        try:
            filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)
            for r in filled:
                r["symbol"] = sym
            all_filled.extend(filled)
        except Exception:
            continue
    if not all_filled:
        return -1.0, 0.0, 1.0, 0, 0.0, 0.0
    # P1-c 宽度顺势加权（可选）：信号日 breadth≥0.4 → avg_pnl×1.5（等效加仓）
    if breadth is not None:
        for r in all_filled:
            bd = _breadth_at(breadth, r["signal_date"])
            if bd is not None and bd >= 0.4:
                r["avg_pnl_pct"] *= 1.5
    pnls = [r["avg_pnl_pct"] for r in all_filled]
    dates = [pd.to_datetime(r["signal_date"]) for r in all_filled]
    kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
    return ann, kelly, curve, len(all_filled), sharpe, max_dd


# [已退役 2026-08-05 SSoT review-fix2] 下列 legacy 入口已整块删除：
#   - STATE_FILE（"logs/param_iter_state.json" 模块级常量）
#   - load_state() / save_state(state)（legacy 冠军治理 JSON 读/写口）
#   - main(argv)（argparse + --legacy fail-closed 守卫的搜索入口）
#   - __main__ 块（force_utf8_stdout + main()）
# 物理意图：参数搜索单一真相源是 discovery daemon（L0-L5）→ experiment.db ACTIVE；
# 任何残留外部调用（grep 全仓库零命中）现已无入口。生产读口统一走
# ``experiment.resolver.resolve_active / resolve_champion``。PARAM_SPACE 仍被
# discovery.sampler 复用（同源候选档，不重造 21 维空间定义）。
