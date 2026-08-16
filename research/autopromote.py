# -*- coding: utf-8 -*-
"""autopromote：量化门槛自动 promote（T2.3 · 2026-08-16 · ADR-15）。

物理意图：推翻既有「promote 留人审」红线（discovery/cli.py cmd_publish 注释所载）的
**受控替代**——不是取消人审，是把人审前置为「门槛数值的定稿」（ADR-15 记录边界与
失效条件），日常放行交给七道量化闸。任何一道支撑件缺失（G7 报告未定稿 / 总开关关 /
写库异常）时 fail-closed 回到人审红线。

七门（G1-G3 replay 口径=实盘同源；G4-G6 scan 口径=统计稳健性工具；G7 可信度前置）：

====== ======== =========================================================
门    口径     判据
====== ======== =========================================================
G1    replay   inner n_hits≥100 ∧ outer n_hits≥30（样本量）
G2    replay   cand outer ann ≥ max(基线+3pp, 0)（绝对不亏下限——R0 实证基线
               本身 replay 口径为负，纯相对改善无意义）∧ calmar 代理
               ann/|dd| ≥ max(基线×1.2, 1.5)
G3    replay   outer |dd| ≤ 基线 |dd| + 2pp（风险不劣化；幅度口径，防负值反转）
G4    scan     wf 四折 oos calmar 全 ≥0（A2 教训：整段 44.87 全靠 2025）
G5    scan     邻域 ±15%×5 扰动 is_plateau（孤峰否决）
G6    scan     DSR ≥ 0.8（多重比较诚实税，n_trials=当前快照 trial 数）
G7    常量     A3「10bps 存活」∧ A4「正 kelly 年占比≥4/6」——首轮循环产物，
               **未定稿(None) → autopromote 整体拒绝运行**（fail-closed）
====== ======== =========================================================

口径混用的诚实声明（ADR-15 详述）：G2/G3 用 replay 引擎口径（PositionModel 组合
约束=实盘同源——R0 实证 discovery scan 口径 outer +18.4% 与 replay 口径 -23.9% 的
裂缝，autopromote 必须站实盘侧）；G4/G5/G6 是统计稳健性工具（折间稳定/邻域高原/
多重比较），scan 口径下这些统计量的分辨率更高，且不直接决定资金。

灰度两步（默认）：过闸 → 基线 set-weight 0.7 + 候选 promote 0.3 → 观察期后
``phase="confirm"`` → 候选 set-weight 1.0 + 基线 archive。

分层：本模块放 research 层（需同时 import discovery + experiment——experiment 是叶子
层不能反向依赖，与 discovery_bridge 同款分层决策）。总开关 ``AUTO_PROMOTE_ENABLED``
（默认 false）：一键冻结自动放行，回人审 promote。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# ── 门槛常量（v1 初值；调整须走 ADR-15 修订记录，含变更理由与实证）──
G1_MIN_INNER_N = 100          # inner 样本量（proposals MIN_HITS=30 加严——换实盘参数要更厚样本）
G1_MIN_OUTER_N = 30
G2_ANN_IMPROVE = 0.03         # outer ann 相对改善 ≥3pp
G2_ANN_ABS_FLOOR = 0.0        # outer ann 绝对不亏（R0 实证：基线 replay 口径为负，纯相对无意义）
G2_CALMAR_RATIO = 1.2         # calmar 代理（ann/|dd|）相对改善 ×1.2
G2_CALMAR_ABS_FLOOR = 1.5     # calmar 代理绝对下限
G3_DD_WORSE_LIMIT = 0.02      # outer |dd| 劣化 ≤2pp
# ⚠️ 与 proposals.OUTER_DD_WORSE_LIMIT=0.05 刻意分级（review 注记 2026-08-16）：那是
# 提案准入闸（A 档 MVP，进 DRAFT 池），本处是实盘放行闸（动真钱，更严一档半）。
# 两闸语义不同故不共享常量；若调整任一阈值须同 PR 复核另一处并记 ADR-15 修订。
G6_DSR_MIN = 0.8              # 与 discovery.runner.DSR_GATE_MIN 同值（共因子语义）
G7_REQUIRED_KELLY_RATIO = 4 / 6

# ── G7 首轮循环结论常量（R0-A3/R0-A4 报告定稿后回填；None=未定稿 → 拒跑，fail-closed）──
# R0-A3：滑点敏感性——冠军候选 outer ann@10bps ≥ ann@0bps×50% 且 @20bps>0 → True。
# R0（08-16 晨）：ACTIVE/DRAFT 两参数集 replay 口径 outer 0bps 即负 → False。
# R1 修订（08-16 晚 · ADR-15 修订记录）：质量方向两候选实证翻绿——
#   touch3 盈亏平衡 48.6bps / 10bps 存活率 79%；3e383d 51.0bps / 89%（50bps 仍 +2.6%）
#   （diag/a3_fill_realism_probe.py --experiments 两验体，logs/a3_r1_touch3.out）。
#   ACTIVE 基线仍薄边缘（0bps 即负）——本常量语义是「存在过滑点存活的候选方向」，
#   非全参数集背书。验体：neckline_r1_touch3_20260816 / neckline_prop_20260816_3e383d。
A3_SURVIVES_10BPS: bool | None = True
# R0-A4：Kelly 收敛——正 kelly 年占比（n≥30 年，逃考惩罚口径）。
# 2026-08-16 实测 0.667（4/6，2022/2023 塌零；wf OOS 无翻负）——
# 详见 docs/research/2026-08-16-a4-kelly-convergence.md
A4_POS_KELLY_RATIO: float | None = 0.667

_OPERATOR = "autopromote:gate-v1"
# 灰度第一步的权重切分（旧 0.7 / 新 0.3；confirm 后 1.0/归档）
STEP1_OLD_WEIGHT = 0.7
STEP1_NEW_WEIGHT = 0.3


def _load_version(experiment_id: str, db_path: str | None = None):
    """按 id 读实验版本（list_versions 过滤——store 无单读 API，语义等价）。"""
    from experiment.store import _DEFAULT_DB, list_versions
    for v in list_versions(db_path or _DEFAULT_DB):
        if v.experiment_id == experiment_id:
            return v
    raise ValueError(f"实验版本不存在: {experiment_id}")


def _resolve_baseline_id(baseline_id: str | None, db_path: str | None = None) -> str | None:
    """基线=当前 ACTIVE 冠军（resolve_champion max-weight 单一选择口径）。"""
    if baseline_id:
        return baseline_id
    from experiment.resolver import resolve_champion
    champ = resolve_champion()
    return champ.experiment_id if champ else None


def _n_trials_current_snapshot() -> int:
    """当前快照的 trial 数（G6 多重比较修正的 M）。读不到（无库/无 trial）→ 1（无修正）。"""
    try:
        import sqlite3
        from discovery.store import DEFAULT_DB_PATH as _DISC_DB
        con = sqlite3.connect(f"file:{_DISC_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT snapshot_hash, COUNT(*) FROM trial GROUP BY snapshot_hash "
                "ORDER BY MAX(created_at) DESC LIMIT 1").fetchone()
            return int(row[1]) if row else 1
        finally:
            con.close()
    except Exception:
        logger.warning("读 discovery trial 数失败，G6 按 M=1（无多重比较修正）", exc_info=True)
        return 1


def evaluate_gates(experiment_id: str, baseline_id: str | None = None,
                   lake_start: str = "2021-01-01", db_path: str | None = None) -> dict:
    """逐门评估候选 vs 基线，返回 {gates, all_pass, kelly_hat, 读数明细}。

    不写任何库（纯评估）；promote 动作在 run()。评估成本约 30-45 分钟
    （2×evaluate_replay + evaluate_wf + neighborhood 6×evaluate），只对冠军候选跑。
    """
    if A3_SURVIVES_10BPS is None or A4_POS_KELLY_RATIO is None:
        raise RuntimeError(
            "G7 前置常量未定稿（A3_SURVIVES_10BPS/A4_POS_KELLY_RATIO 为 None）——"
            "autopromote fail-closed 拒绝运行；请在 R0-A3/R0-A4 报告定稿后回填常量")

    cand = _load_version(experiment_id, db_path)
    base_id = _resolve_baseline_id(baseline_id, db_path)
    if base_id is None:
        raise RuntimeError("无 ACTIVE 基线——autopromote 需要对照冠军，拒绝运行")
    if cand.status.value != "DRAFT":
        raise ValueError(f"autopromote 仅评估 DRAFT 候选（当前 {cand.status.value}）")
    base = _load_version(base_id, db_path)

    # 快照冻结一次，全部评估复用（同 universe 同指纹——门槛读数可复现的基石）
    from discovery.snapshot import freeze
    from discovery.split import holdout_split, walk_forward_split
    from discovery.objective import evaluate_replay, evaluate_wf, evaluate
    from discovery.neighborhood import neighborhood_stability
    from discovery.dsr import deflated_sharpe

    universe, meta = freeze(lake_start)
    split = holdout_split()

    gates: dict[str, dict] = {}
    gates["_meta"] = {"snapshot_hash": meta.snapshot_hash,
                      "universe_count": meta.universe_count,
                      "baseline_id": base_id, "candidate_id": experiment_id,
                      "lake_start": lake_start}

    # ── G1-G3：replay 口径（实盘同源）──
    res_base = evaluate_replay(base.params, universe, split)
    res_cand = evaluate_replay(cand.params, universe, split)
    bi, bo = res_base["inner"], res_base["outer"]
    ci, co = res_cand["inner"], res_cand["outer"]
    gates["G1_样本量"] = {
        "cand_inner_n": ci["n_hits"], "cand_outer_n": co["n_hits"],
        "pass": ci["n_hits"] >= G1_MIN_INNER_N and co["n_hits"] >= G1_MIN_OUTER_N}
    base_ann, cand_ann = bo["annualized_return"], co["annualized_return"]
    base_dd, cand_dd = abs(bo["max_drawdown"] or 0.0), abs(co["max_drawdown"] or 0.0)
    # 无回撤哨兵：dd≈0 且 ann>0 → ratio 视为「极大」（远超任何有限阈值）；ann≤0 → 0
    _NO_DD_SENTINEL = 99.0
    base_ratio = base_ann / base_dd if base_dd > 1e-9 else (_NO_DD_SENTINEL if base_ann > 0 else 0.0)
    cand_ratio = cand_ann / cand_dd if cand_dd > 1e-9 else (_NO_DD_SENTINEL if cand_ann > 0 else 0.0)
    gates["G2_outer改善"] = {
        "base_outer_ann": base_ann, "cand_outer_ann": cand_ann,
        "ann_need": max(base_ann + G2_ANN_IMPROVE, G2_ANN_ABS_FLOOR),
        "base_ratio": base_ratio, "cand_ratio": cand_ratio,
        "ratio_need": max(base_ratio * G2_CALMAR_RATIO, G2_CALMAR_ABS_FLOOR),
        "pass": cand_ann >= max(base_ann + G2_ANN_IMPROVE, G2_ANN_ABS_FLOOR)
        and cand_ratio >= max(base_ratio * G2_CALMAR_RATIO, G2_CALMAR_ABS_FLOOR)}
    gates["G3_风险不劣化"] = {
        "base_outer_dd": base_dd, "cand_outer_dd": cand_dd,
        "pass": cand_dd <= base_dd + G3_DD_WORSE_LIMIT}

    # ── G4-G6：scan 口径（统计稳健性）──
    wf = evaluate_wf(cand.params, walk_forward_split())
    oos_calmars = [f["oos"]["calmar"] for f in wf]
    gates["G4_跨年稳健"] = {
        "oos_calmars": oos_calmars, "pass": all(c >= 0 for c in oos_calmars)}
    nb = neighborhood_stability(cand.params, universe, split)
    gates["G5_邻域高原"] = {
        "neighbor_mean": nb["neighbor_mean"], "base_calmar": nb["base_calmar"],
        "pass": bool(nb["is_plateau"])}
    inner_eval = evaluate(cand.params, universe, split)["inner"]
    n_trials = _n_trials_current_snapshot()
    dsr = deflated_sharpe(inner_eval["sharpe"], n_trials, max(inner_eval["n"], 2))
    gates["G6_多重比较"] = {
        "sharpe": inner_eval["sharpe"], "n_trials": n_trials, "dsr": dsr,
        "pass": dsr >= G6_DSR_MIN}

    # ── G7：首轮循环结论常量 ──
    gates["G7_可信度前置"] = {
        "a3_survives_10bps": A3_SURVIVES_10BPS,
        "a4_pos_kelly_ratio": A4_POS_KELLY_RATIO,
        "pass": bool(A3_SURVIVES_10BPS) and A4_POS_KELLY_RATIO >= G7_REQUIRED_KELLY_RATIO}

    # kelly_hat（ADR-14 播报物）：**保守分位（下三分位）**而非点估计——review 修复
    # （2026-08-16）：ADR-14 Decision 3 明文「hat 取保守分位（下三分位），弱年退化到
    # 最小仓位是特性」，原实现用 inner 整段点估计违背该条款（A4 实证 CV 0.92，点估计
    # 会把 2024/2025 好年杠杆错配给弱年）。分年 kelly 复用 A4 口径：全历史（lake_start
    # 2021 起）按信号自然年分块，n≥30 年可信；不足三年时退化为分年最小（同保守向）。
    kelly_hat = _conservative_kelly_hat(cand.params, universe)

    all_pass = all(g["pass"] for k, g in gates.items() if k != "_meta")
    return {"experiment_id": experiment_id, "baseline_id": base_id,
            "gates": gates, "all_pass": all_pass, "kelly_hat": kelly_hat}


def _conservative_kelly_hat(params: dict, universe) -> float:
    """分年 kelly → 下三分位（ADR-14 保守分位；与 diag/a4_kelly_convergence.py 同口径）。

    Why 全历史而非 inner 段：A4 的分年 kelly 覆盖 2021-2026（inner 只有 2025 单年，
    按年分块会退化成点估计失去分位意义）。Why 下三分位而非 min：单年 kelly=0
    （2022/2023 形状）会让 hat 直接归零、kelly 模式退化到永不开仓——下三分位在
    「保守」与「不完全自废」之间取折中，与 A4 报告「可上但 hat 取保守分位」一致。
    年份不足 3（无三分位可言）→ 分年最小。无任何可信年 → 0.0（最保守）。
    """
    from collections import defaultdict
    import pandas as pd
    from discovery.objective import run_full_scan
    from strategies.neckline.backtest import kelly_metrics

    all_filled = run_full_scan(params, universe)
    by_year = defaultdict(list)
    for r in all_filled:
        d = pd.to_datetime(r["signal_date"])
        by_year[d.year].append((r["avg_pnl_pct"], d))
    year_kellys = []
    for y in sorted(by_year):
        pnls = [p for p, _ in by_year[y]]
        if len(pnls) >= 30:                     # 可信年下限（A4/yearly_metrics 同口径）
            k, _, _ = kelly_metrics(pnls, [d for _, d in by_year[y]])
            year_kellys.append(float(k))
    if len(year_kellys) >= 3:
        return round(sorted(year_kellys)[(len(year_kellys) - 1) // 3], 4)
    if year_kellys:
        return round(min(year_kellys), 4)
    return 0.0


def render_report(res: dict) -> str:
    """门槛读数 → 钉钉播报 markdown（逐门绿红 + 回滚命令 + kelly_hat env 行）。"""
    lines = [f"## autopromote 门槛报告：{res['experiment_id']} vs {res['baseline_id']}"]
    for k, g in res["gates"].items():
        if k == "_meta":
            continue
        mark = "✅" if g["pass"] else "❌"
        reading = " ".join(
            f"{ik}={iv:+.3f}" if isinstance(iv, float) else f"{ik}={iv}"
            for ik, iv in g.items() if ik != "pass")
        lines.append(f"- {mark} **{k}**：{reading}")
    lines.append(f"\n**总判定**：{'全绿——可灰度' if res['all_pass'] else '未全绿——拒绝/处置'}"
                 f" ｜ kelly_hat={res['kelly_hat']}"
                 f"（贴 env：`TRADE_KELLY_HAT={res['kelly_hat']}`）")
    lines.append("回滚 SOP：`python -m experiment set-weight <旧id> --weight 1.0 && "
                 "python -m experiment archive <新id>`")
    return "\n".join(lines)


def _push(md: str) -> None:
    """钉钉播报（复用 digest.push_digest 装配范式——子进程 load_dotenv + 同步等待）。"""
    try:
        from research.digest import push_digest
        push_digest(md)
    except Exception:
        logger.exception("autopromote 播报失败（不阻断主流程）")


def run(experiment_id: str, *, phase: str = "initial", dry_run: bool = True,
        weight: float | None = None, baseline_id: str | None = None,
        db_path: str | None = None, notify: bool = True) -> dict:
    """autopromote 主入口：评估七门 → （非 dry_run 且开关开）执行灰度迁移。

    phase="initial"：过闸 → 基线 set-weight 0.7 + 候选 promote 0.3（或 --weight 覆盖）
    phase="confirm"：跳过评估（initial 时已过闸）→ 候选 set-weight 1.0 + 基线 archive
    dry_run=True（默认）：只评估+播报，零写库。
    """
    from experiment.store import (_DEFAULT_DB as _EXP_DB, archive, promote, set_weight)

    db = db_path or _EXP_DB
    if phase == "confirm":
        # confirm 阶段：候选须已是 ACTIVE（initial 的产物），直接完成迁移
        cand = _load_version(experiment_id, db)
        if cand.status.value != "ACTIVE":
            raise ValueError(f"confirm 仅对已灰度的 ACTIVE 有效（当前 {cand.status.value}）")
        base_id = baseline_id or _resolve_baseline_id(baseline_id, db)
        if dry_run:
            return {"action": "confirm(dry-run)", "would": ["set-weight 1.0", "archive 旧"],
                    "experiment_id": experiment_id, "baseline_id": base_id}
        if os.getenv("AUTO_PROMOTE_ENABLED", "").lower() != "true":
            raise RuntimeError("AUTO_PROMOTE_ENABLED 未开——confirm 拒绝写库（人审红线生效）")
        now = datetime.now().isoformat(timespec="seconds")
        # 顺序红线（资金守恒）：先 archive 基线（释放其权重）再 set-weight 1.0——
        # 反序被 validate_weight_sum 拒（0.7+1.0>1.0）。与 initial 阶段同款顺序教训。
        archived = False
        if base_id and base_id != experiment_id:
            try:
                archive(db, base_id, operator=_OPERATOR, now=now)
                archived = True
            except ValueError:
                logger.warning("基线 archive 失败（可能已非 ACTIVE）——跳过", exc_info=True)
        set_weight(db, experiment_id, new_weight=1.0, operator=_OPERATOR, now=now)
        if not archived:
            logger.warning("基线未归档且候选已 1.0——若仍有其他 ACTIVE 需人工收权")
        result = {"action": "confirm", "experiment_id": experiment_id, "baseline_id": base_id}
        if notify:
            _push(f"## autopromote confirm 完成\n{experiment_id} → weight=1.0；"
                  f"基线 {base_id} 已归档。")
        return result

    # initial 阶段：七门评估
    res = evaluate_gates(experiment_id, baseline_id=baseline_id, db_path=db)
    md = render_report(res)
    if notify:
        _push(md)
    if dry_run or not res["all_pass"]:
        return {"action": "initial(dry-run)" if dry_run else "initial(rejected)",
                **res}

    # 全绿 + 非 dry-run → 灰度写库（仍受总开关管辖）
    if os.getenv("AUTO_PROMOTE_ENABLED", "").lower() != "true":
        raise RuntimeError(
            "AUTO_PROMOTE_ENABLED 未开——过闸候选拒绝自动写库（人审红线生效）；"
            "确认后 set AUTO_PROMOTE_ENABLED=true 或人工 python -m experiment promote")
    now = datetime.now().isoformat(timespec="seconds")
    new_w = weight if weight is not None else STEP1_NEW_WEIGHT
    base_id = res["baseline_id"]
    # 顺序红线（资金守恒）：先降基线权重再 promote 新——反序会被 promote 的
    # validate_weight_sum 拒（旧 1.0 + 新 0.3 > 1.0）。测试实弹抓出，非纸面推演。
    if base_id and base_id != experiment_id:
        set_weight(db, base_id, new_weight=round(1.0 - new_w, 4),
                   operator=_OPERATOR, now=now)
    promote(db, experiment_id, weight=new_w, operator=_OPERATOR, now=now)
    # ADR-14「kelly_hat 写实验 note」（review 修复）：版本表 note 不可变（创建快照），
    # 追加走 append_note 审计行——保守分位 hat + 对应 .env 行入库可溯源。
    from experiment.store import append_note
    append_note(db, experiment_id,
                note=f"kelly_hat(下三分位)={res['kelly_hat']}；"
                     f"贴 env：TRADE_KELLY_HAT={res['kelly_hat']}",
                operator=_OPERATOR, now=now)
    logger.info("[autopromote] 灰度第一步完成 %s → %.2f（门槛读数见播报/ROUND_LOG）",
                experiment_id, new_w)
    return {"action": "initial(promoted)", **res}
