# -*- coding: utf-8 -*-
"""L3 约束裁剪（spec §7.1，纯函数过滤器，零新增依赖，ADR4 反魔法）。

物理意图：21 维笛卡尔积里有 6 处物理无意义的耦合组合（trailing 互锁 / 死参数 / 退化 / 冲突 /
同开关 / 挂单区间空）。裁剪掉再搜——既省算力（不白跑废组合），也让随机/Sobol 采样更高效
（合法密度提升），是不第一刀上 optuna 的前提（spec §7.2）。

Plan 2 实现范围（诚实收窄）：
- 耦合 1（trailing 互锁）：normalize_params 强制 grace=0 时 step/floor 固定 ✓
- ~~耦合 2（min_rr 死参数）~~：P4（2026-08-13）**撤除**——「结构恒 rr=2.0」是 R3 前
  （几何 rr 2H/H）的旧口径；R3（2026-07-27）改实际口径 (tp2−entry)/(entry−stop_price)
  后 min_rr 是活参数（边际效应实测三档均值 5.16/10.24/7.76，非零）。P3 敏感性分析
  同时发现 TPE 采样绕过 normalize（Sobol 强制 2.0 而 TPE 自由 1.0/1.5/2.0）——搜索
  空间与评估空间不一致。收口：normalize 不再动 min_rr，TPE 建议经 normalize+is_feasible
  重抽（tpe_search_batch），min_rr 按候选档正常搜索。
- ~~耦合 3（tp1 ≤ tp_h）~~：R6-8（2026-08-26）**撤除**——旧判据「止盈1 比止盈2
  还远无意义」基于修复前语义：tp1>tp_h 时 lot1 按从未到达的 tp1 价位记账=幽灵成交
  （R6-1 实锤，读数全是垃圾，禁区合理）。R6-4 修复（lot1 按当根真实到达价位记账）
  后该区语义合法且为**当前冠军所在区**（supp04+tp1_h=2.0 > tp_h=1.5，真值
  outer +115.4%）：tp1 限价挂 tp2 之上，首触 tp2 当根强势摸到 tp1 则 lot1 按
  tp1 成交，否则随 tp2 同价平——「强势日多拿一段」的动量条件减仓。禁区不撤，
  TPE 永远搜不到 R6-4 后的最优区（R5-P3 无一超旧前沿的结构性原因之一）。
- 耦合 4（cancel ≥ tp1，None=放飞合法）：is_feasible 静态判定 ✓（保持——pending
  期撤单价低于一档目标的组合仍判过保守；当前冠军 cancel=None 不受阻，语义厘清
  defer 后续）
- 耦合 5（suppression ↔ decay_tau 同开关）：**Plan 2 不裁剪**——代码实证二者独立可调
  （decay_tau=None 等权时 suppression 仍生效），spec §7.1 原文"捆绑调"语义在实证下退化为
  "都可调"，凭空裁剪会误杀合法组合。留 Plan 3 语义厘清后再定。
- 耦合 6（buy_limit < cancel×H/ATR 挂单区间非空）：**Plan 2 不裁剪**——H/ATR 是 runtime
  数据（每标的每信号点不同），采样期无法静态判定。留 Plan 3 runtime 裁剪（worker 内判）。
"""
# 21 维参数键（与 discovery/objective.ID_KEYS+EXEC_KEYS 同源；本模块自带避免循环依赖）
PARAM_KEYS = [
    # 识别层 11 维
    "window", "min_touches", "min_suppression", "local_extrema_window",
    "min_bottoms", "breakout_vol_mult", "min_rr", "max_h_atr",
    "stop_atr_mult", "tp_h_mult", "decay_tau",
    # 执行层 7 维
    "max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
    "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
    # trailing 3 维
    "trailing_grace", "trailing_step", "trailing_floor",
]

# 耦合1：grace=0 时 trailing 不激活，step/floor 固定为基线（取任何值都不生效）
TRAILING_OFF_STEP = 0.0
TRAILING_OFF_FLOOR = 0.0


def normalize_params(params):
    """规范化参数：trailing 互锁处置（P4 撤 min_rr 死参强制后仅剩此耦合）。

    - trailing_grace=0 时 step/floor 强制 OFF 基线（耦合1，trailing 不生效，搜它们白跑）。
    - trailing_grace>0 时 step/floor 保留（trailing 激活，搜索有效）。
    - min_rr 不再强制（P4 · 2026-08-13）：R3 实际口径后为活参数，按候选档正常搜索。
    返回新 dict（不改原 params，纯函数）。

    # 耦合5（suppression↔decay_tau，spec §7.1）厘清（design 决策5）：
    # 代码实证（method_v0.py:163-173）二者独立可调——decay_tau=None 等权时 suppression 仍
    # 生效，spec 原文"捆绑调"语义退化为"都可调"。凭空裁剪会误杀合法组合，故 Plan 3 仅文档
    # 厘清，不在 normalize/is_feasible 强制捆绑（与耦合1-4 的硬裁剪区分）。
    """
    p = dict(params)
    if p.get("trailing_grace", 0) == 0:
        p["trailing_step"] = TRAILING_OFF_STEP
        p["trailing_floor"] = TRAILING_OFF_FLOOR
    return p


def is_feasible(params):
    """静态可行性判定（耦合 4；params 应已 normalize）。

    - ~~耦合3 tp1≤tp_h~~：R6-8（2026-08-26）撤除——R6-4 幽灵成交修复后 tp1>tp_h
      为合法且有据区域（新冠军 supp04+tp1=2.0 所在），旧禁区基于修复前的幽灵语义。
    - 耦合4：cancel_thresh_mult ≥ tp1_h_mult（防 cancel<tp1 过保守——未到 tp1 就撤单
      等于放弃突破）；cancel=None 视为放飞不撤（颈线法默认语义），合法。
    trailing 在 normalize 后已处置，此处不判（grace=0 时 step/floor 不参与判定）。
    """
    tp1 = params.get("tp1_h_mult", 0)
    cancel = params.get("cancel_thresh_mult", None)
    if cancel is not None and cancel < tp1:
        return False
    return True


def filter_feasible(params_iter):
    """约束裁剪整批：normalize → is_feasible → 保留合法组合。

    采样层（sampler）产出原始 params 流后调本函数裁剪，再送 worker 评估。
    返回 list（已 normalize），顺序与输入一致（合法项原序保留）。
    """
    out = []
    for p in params_iter:
        np_ = normalize_params(p)
        if is_feasible(np_):
            out.append(np_)
    return out
