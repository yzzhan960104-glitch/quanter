# -*- coding: utf-8 -*-
"""research/committee/battle_run.py —— 会战验证驱动器（2026-09-06 H4.5-6.5）。

执行 PM 裁决的三提案，遵守 PM 自己的协议约束（不是简单网格）：
  P1 ts8 复审：day 7/8/9 敏感性 inner 扫描 → 提案固定 ts=8 走 verify。
    **不自动 publish**——ts8 曾于 09-01 被用户裁决 APPROVED→DRAFT 搁置待人审，
    本轮只补强证据（敏感性+新 grace 数学），终裁权留人。
  P3 trailing 单旋钮：PM 明令单臂独立验证——grace{7,8} 与 floor{0.75,1.0}
    各自单独成臂（禁笛卡尔组合），最优单臂走 create→verify→APPROVED 才
    publish DRAFT（explore_loop 同款止步红线）。
  P2 非 above 区 sizing 粗帽：NecklineConfig 无仓位参数=真 B 档，落 PENDING。
  P0 rr 口径定谳：源码级已证（strategy.py:188 rr=已实现R倍数），此处只登记。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from research import proposals as RP
from research.committee import triage as TR

DB = str(ROOT / "logs" / "research_proposals.db")
OUT = ROOT / "logs" / "committee_20260906"


def main() -> int:
    from experiment.resolver import resolve_champion
    from discovery.snapshot import freeze
    from discovery.split import holdout_split

    t0 = datetime.now()
    champ = resolve_champion()
    baseline_params = dict(champ.params)
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    rep: dict = {"started_at": f"{t0:%H:%M:%S}", "champion": champ.experiment_id,
                 "rr_verdict": "语料 rr=avg_pnl_pct/risk_pct=已实现R倍数（事后），"
                               "strategy.py:188 源码定谳；信号时 rr 在识别层 min_rr 把守"}

    def inner(partial: dict) -> dict:
        return TR._evaluate_inner(partial, baseline_params, universe, split)

    keys = ("n_hits", "win_rate", "avg_rr", "max_drawdown", "annualized_return")
    print("[battle] 基线 inner …", flush=True)
    base = inner({})
    rep["baseline_inner"] = {k: base.get(k) for k in keys}
    print(f"[battle] baseline {rep['baseline_inner']}", flush=True)

    # ── P1：ts8 复审（敏感性 7/8/9；提案固定 8；不 publish） ──
    p1_cells = {}
    for ts in (7, 8, 9):
        m = inner({"time_stop_days": ts})
        p1_cells[ts] = {k: m.get(k) for k in keys}
        print(f"[battle] P1 ts={ts} → {p1_cells[ts]}", flush=True)
    rep["p1_sensitivity"] = p1_cells
    pid1 = RP.create_proposal(
        DB, change_type="A",
        hypothesis="time_stop_days=8 复审：day8 强平磨仓笔，关闭 trailing 不可达"
                   "的 8-15 日净亏窗（-97,213pp）；grace10 使交叠仅 11-15 日 4 天，"
                   "原搁置理由被数学削弱",
        params={"time_stop_days": 8},
        expected_effect="方向为正、量级不预设（六窗 replay 4正1平1微负先例；"
                        "桶算术只作动机）",
        risk="桶边界过拟合 + 截断慢赢家（8-15日 tp2 腿 +91,727pp 被重定价）",
        note="committee20260906:P1 ts8 复审｜09-01 曾 APPROVED→DRAFT 用户裁决"
             "搁置，本轮只补强证据不 publish，终裁留人审")
    ok1 = RP.verify_proposal(DB, pid1)
    v1 = RP.get_proposal(DB, pid1)
    rep["p1"] = {"proposal_id": pid1, "verified": ok1,
                 "status": v1["status"], "verification": v1.get("verification_json"),
                 "published": "NO（尊重 09-01 用户裁决，待人审）"}
    print(f"[battle] P1 verify={ok1} status={v1['status']}", flush=True)

    # ── P3：trailing 单旋钮（单臂独立，禁组合） ──
    arms = {
        "trailing_grace=7": {"trailing_grace": 7},
        "trailing_grace=8": {"trailing_grace": 8},
        "trailing_floor=0.75": {"trailing_floor": 0.75},
        "trailing_floor=1.0": {"trailing_floor": 1.0},
    }
    p3_cells = {}
    for name, cell in arms.items():
        m = inner(cell)
        p3_cells[name] = {k: m.get(k) for k in keys}
        print(f"[battle] P3 {name} → {p3_cells[name]}", flush=True)
    rep["p3_single_arms"] = p3_cells
    base_ann = base.get("annualized_return") or 0.0
    improved = {n: m for n, m in p3_cells.items()
                if (m.get("annualized_return") or -9) > base_ann
                and (m.get("n_hits") or 0) >= RP.MIN_HITS}
    if improved:
        best_arm = max(improved, key=lambda n: improved[n]["annualized_return"])
        best_cell = arms[best_arm]
        pid3 = RP.create_proposal(
            DB, change_type="A",
            hypothesis=f"trailing 单旋钮收紧（{best_arm}）：提前/加深收紧，削减"
                       "跳空恶化尾部（2026 stop 均 -11.95% 六年最差）；PM 协议="
                       "单臂独立验证，禁多旋钮组合",
            params=best_cell,
            expected_effect="day16 理论止损约降 0.25ATR，量级小于 P1；对 16-30 桶"
                            "（净 -28,315pp）部分覆盖",
            risk="抖出颈线回踩赢家；桶瞄准调参（R10 同型）",
            note=f"committee20260906:P3 单臂择优（4 臂：grace7/8、floor0.75/1.0，"
                 f"inner 扫描各臂独立）；若 P1 过人审本提案应降级/撤回")
        ok3 = RP.verify_proposal(DB, pid3)
        v3 = RP.get_proposal(DB, pid3)
        pub3 = None
        if ok3:
            pub3 = RP.publish_proposal(DB, pid3)
        rep["p3"] = {"proposal_id": pid3, "best_arm": best_arm,
                     "all_arms": p3_cells, "verified": ok3,
                     "status": v3["status"], "verification": v3.get("verification_json"),
                     "published_experiment": pub3}
        print(f"[battle] P3 best={best_arm} verify={ok3} → {v3['status']}"
              f"{' published ' + str(pub3) if pub3 else ''}", flush=True)
    else:
        rep["p3"] = {"verdict": "四单臂 inner 均未超基线——不立提案（fail-closed）"}
        print("[battle] P3 四臂均未超基线，不立提案", flush=True)

    # ── P2：B 档 PENDING（sizing 新键属结构级，人审域） ──
    pid2 = RP.create_proposal(
        DB, change_type="B",
        hypothesis="非 above 区（创业板指<MA60）仓位粗帽：并发上限减半优先于"
                   "条件乘数。动机=尾部/截面共振保险（19仓全deep最坏-23%）而非"
                   "期望（×0.5 全期仅约+1,937pp）。与 2026-09-05 三区节流设计"
                   "（缠线带停新仓）同族，须合并人审",
        params=None,
        expected_effect="负 regime 年回撤与单日止损共振收缩；期望增益微",
        risk="deep 非平稳（2026 已翻负 -1.22%/笔 vs 全窗 -0.23%），V 反年误伤；"
             "sizing 层无现成参数键=结构级改动",
        note="committee20260906:P2｜验证要求：全窗组合口径 equity 重放+deep/wire"
             "×年分解+外样本零损；NecklineConfig 无仓位键须先扩 schema")
    rep["p2"] = {"proposal_id": pid2, "status": "PENDING（B 档人审）"}

    rep["finished_at"] = f"{datetime.now():%H:%M:%S}"
    out = OUT / "battle_validation.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"[battle] 完成 → {out}（耗时 "
          f"{(datetime.now() - t0).total_seconds():.0f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
