# -*- coding: utf-8 -*-
"""L3 搜索层完成·TPE 序贯优化（spec §7.2 阶段二，optuna）。

物理意图（spec §7.2 v1.1）：v1 把 TPE 当"可选增强"是 L3 空心根源。v1.1 确立"Sobol 准随机
初始覆盖 → TPE 序贯优化"两阶段。Plan 2 已立 Sobol 初始覆盖（判据④覆盖度的物理手段）；
本模块做阶段二——在 Sobol 撒的点上拟合 TPE 后验，向预期提升 EI 高的区域集中采样。

关键设计（design 决策1/3）：
1. TPE 目标 = inner **calmar**（objective_fn 返回 calmar）。spec §6.2 v1.1 写"objective=ann"，
   但 §3.5 v1.2 主排序改 calmar，ann 是 risk_metrics 复利放大失真产物（§1.4 实证夏普15/ann201%）。
   Plan 3 让 TPE 跟随 v1.2 用 calmar（TPE 优化什么就排序什么）。
2. Plan 2 自写 Sobol 不废弃——`study.enqueue_trial` 注入 Sobol 点 warm start TPE，
   对齐 §7.2"Sobol 初始覆盖→TPE"两阶段。
3. TPE 序贯需前序结果（每次采基于历史），**主进程串行 evaluate**（不 spawn 子进程）。
   与 Plan 2 Sobol 阶段的 ProcessPool 并发区分——TPE 阶段是精化，串行可接受（夜跑预算内）。

反魔法（ADR4）：optuna 是 spec §7.2 唯一新依赖，轻量通用优化框架（非 vectorbt/backtrader
等量化黑盒红线）。TPE 算法用 optuna 成熟实现（自写双 GMM l(x)/g(x)+EI 最大化数值 bug 风险高）。
"""
import optuna

DEFAULT_N_TPE_TRIALS = 20


def _nearest_in_candidates(v, cands):
    """v 映射到 cands 最近邻（normalize 执行值如 0.0/2.0 可能越界 optuna choices，enqueue 前兜底）。

    物理意图：normalize_params 把 trailing_step=0.0（grace=0 不激活哨兵）/min_rr=2.0（死参）
    等执行值固定，但这些值可能不在 PARAM_SPACE 候选档内。optuna suggest_categorical 要求
    enqueue 值 ∈ choices，越界则 ValueError。snap 到最近邻候选档——TPE 只在候选档空间采样，
    真实执行语义由 evaluate 内 simulate_exit 的 grace>0 AND step>0 条件保证（grace=0 时
    step 不论候选档值都不激活），故 snap 不改物理行为。
    """
    if v in cands:
        return v
    # 数值最近邻（排除 bool，bool 是 int 子类）
    numeric = [c for c in cands if isinstance(c, (int, float)) and not isinstance(c, bool)]
    if numeric and isinstance(v, (int, float)) and not isinstance(v, bool):
        return min(numeric, key=lambda c: abs(c - v))
    return cands[0]   # None/分类越界兜底首档


def _snap_to_candidates(params, param_space):
    """把 params 每维值 snap 到候选档（enqueue 前调用，防 normalize 越界值 crash）。"""
    return {k: _nearest_in_candidates(params.get(k), cands) for k, cands in param_space}


def tpe_search(seed_params, objective_fn, n_trials=DEFAULT_N_TPE_TRIALS,
               seed=42, param_space=None):
    """optuna TPESampler 序贯优化 + Sobol warm start。

    seed_params: warm start 点（Plan 2 sample_search 产，list[dict] 21 维）。
    objective_fn(params) -> float: 返回 inner calmar（最大化；T6 runner 闭包 universe 提供）。
    n_trials: TPE 新采 trial 数（不含 seed）。
    param_space: [(key, [candidates]), ...]，默认 sampler.PARAM_SPACE（21 维离散档）。
    返回 (all_params, study)：all_params = seed + tpe 全部 trial 的 params（list[dict]）；
      study = optuna study（T6 读 best_value / 给 expected_improvement）。

    离散采样：每维 trial.suggest_categorical（颈线法参数是离散候选档，非连续）。
    """
    if param_space is None:
        from discovery.sampler import PARAM_SPACE
        param_space = PARAM_SPACE
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    # warm start：enqueue Plan 2 Sobol 点（snap 到候选档——normalize 越界值如 trailing_step=0.0
    # 不在 choices 内，optuna suggest_categorical 会 ValueError，故 enqueue 前 snap 最近邻）
    for p in seed_params:
        study.enqueue_trial(_snap_to_candidates(p, param_space))

    def obj(trial):
        # 离散档采样（enqueue 的 trial 用既有值，suggest_categorical 声明 search space）
        params = {k: trial.suggest_categorical(k, cands) for k, cands in param_space}
        return objective_fn(params)

    total = len(seed_params) + n_trials
    study.optimize(obj, n_trials=total)
    all_params = [{k: t.params[k] for k, _ in param_space} for t in study.trials]
    return all_params, study


def expected_improvement(study, window=10):
    """判据②代理（spec §3.5 EI<ε）：最近 window trial 内 best_value 改进幅度。

    optuna TPESampler 内部 EI 不暴露 API，用代理——最近 window trial 的 value 极差
    （max - window 起点）。极差≈0 = best 不涨 = 没有预期能改进的点（EI 衰减）。
    调用方按 ε 阈值判（如 ε=1e-3）。trial 数 <2 保守返回 inf（不停，让 TPE 多跑）。
    """
    values = [t.value for t in study.trials if t.value is not None]
    if len(values) < 2:
        return float("inf")
    recent = values[-min(window, len(values)):]
    return max(recent) - recent[0]
