# -*- coding: utf-8 -*-
"""归因驱动定向参数探索环（attribution-driven exploration · 2026-09-03）。

物理意图：把 18:05 亏损归因的 LLM 意见（param_directions）自动翻译成**研究
侧定向参数扫描**——意见→网格→逐格回测（inner 窗）→探索报告→最优格走既有
提案流（create→verify→APPROVED 才 publish DRAFT）。

安全边界（三条红线，与 08-25「自动参数优化全停」裁决对齐）：
  - **止步 DRAFT**：绝不 promote——晋升仍走 autopromote 七门 dry-run + 人审 CLI
  - **探索面=NecklineConfig**（replay cfg_override 可覆盖面）；掘金腿
    TRADE_CFG/R10_FILTERS/硬闸不在面内（测不了就不假装能测）
  - **升格门槛=_judge 同款**（research.proposals 的准入闸单源复用，不自立标准）

时序：18:05 归因 → 18:30 digest/提案 → **18:45 本环**（探索 20-40 分钟）。
产物：logs/explore_{day}.json + md；探索摘要自动注入次日 digest md（提案
LLM 的学习回路）；CLI：python -m research.explore_loop [--day --force]。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

OUT_DIR = ROOT / "logs"
JOB_NAME = "ops_explore_loop"

# ─────────────────────── 意见→网格：领域映射表 ───────────────────────
# 归因主因关键词（loser_review prompt 词表的子集）→ (参数键, 探索网格比)。
# 网格=冠军基线值 × ratios（相对邻域，比率 1.0=基线自身不重跑）。
_KEY_RATIOS: dict[str, tuple[float, ...]] = {
    "stop_atr_mult": (0.70, 0.85, 1.15, 1.30),
    "buy_limit_atr_mult": (0.70, 0.85, 1.15, 1.30),
    "breakout_vol_mult": (1.0, 1.15, 1.3, 1.5, 1.8),
    "min_rr": (1.0, 1.1, 1.25, 1.4, 1.6),
    "momentum_gate": (0.5, 0.75, 1.5, 2.0),
    "max_holding": (0.6, 0.8, 1.2, 1.4),
    "trailing_grace": (0.5, 0.75, 1.25, 1.5),
}
_PRIMARY_GRIDS: dict[str, str] = {          # 归因主因关键词 → 参数键
    "止损": "stop_atr_mult",
    "入场时机": "buy_limit_atr_mult",
    "入场即逆风": "breakout_vol_mult",
    "系统性": "min_rr",
    "个股趋势": "momentum_gate",
    "流动性": "max_holding",
    "持有超期": "max_holding",
}

# LLM param_directions 的键白名单（NecklineConfig 子集——LLM prompt 候选表）
_LLM_KEYS = set(_KEY_RATIOS)


def plan_grids(review_doc: dict, champion_params: dict) -> list[dict]:
    """归因意见 → 探索网格清单（纯函数）。

    优先级：① 当日全部归因行 LLM param_directions 的键频次 top2（键必须过
    _LLM_KEYS 白名单）② 回落全部主因的关键词映射 top2。每维一网格（比率×4-5），
    网格值与冠军值相同者剔除（不重跑基线）。最多 2 维（成本控制 ~20-30min）。
    """
    from collections import Counter
    key_freq: Counter = Counter()
    for leg in review_doc.get("legs") or []:
        for r in leg.get("rows") or []:
            for k in ((r.get("analysis") or {}).get("param_directions") or {}):
                if k in _LLM_KEYS:
                    key_freq[k] += 1
    keys = [k for k, _ in key_freq.most_common(2)]
    if not keys:                                   # 回落：主因关键词映射
        seen: Counter = Counter()
        for leg in review_doc.get("legs") or []:
            for r in leg.get("rows") or []:
                p = str((r.get("analysis") or {}).get("primary") or "")
                for kw, k in _PRIMARY_GRIDS.items():
                    if kw in p and k in champion_params:
                        seen[k] += 1
        keys = [k for k, _ in seen.most_common(2)]
    grids = []
    for k in keys:
        base = champion_params.get(k)
        if base is None or k not in _KEY_RATIOS:
            continue
        vals = sorted({round(float(base) * r, 4) for r in _KEY_RATIOS[k]})
        if vals:
            grids.append({"key": k, "base": float(base), "values": vals})
    return grids


def render_loser_section(day: str | None = None) -> str:
    """当日实盘亏损归因 → digest md 段（提案 LLM 的实盘视野输入）。

    digest main 在 generate_proposal 之前调用；产物缺失返回空串（降级不影响
    digest 主链）。
    """
    day = day or f"{datetime.now():%Y-%m-%d}"
    src = OUT_DIR / f"loser_review_{day}.json"
    if not src.exists():
        return ""
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    lines = [f"\n## 实盘亏损归因（{day} · 浮亏 {sum(len(l.get('rows') or []) for l in doc.get('legs') or [])} 只）"]
    for leg in doc.get("legs") or []:
        rows = sorted(leg.get("rows") or [],
                      key=lambda r: abs(r.get("fpnl_pct") or 0), reverse=True)[:3]
        for r in rows:
            a = r.get("analysis") or {}
            pd_ = a.get("param_directions") or {}
            pd_s = "；".join(f"{k}={v}" for k, v in pd_.items()) or "—"
            lines.append(
                f"- {leg.get('label')} {r.get('symbol')} {r.get('name')} "
                f"亏{r.get('fpnl_pct')}%（持{r.get('days_held')}日）："
                f"{a.get('primary') or a.get('error') or '—'}｜探索方向：{pd_s}")
    return "\n".join(lines) + "\n"


# ─────────────────────── 主链：扫描→报告→升格 ───────────────────────

def _evaluate_inner(params: dict, universe, split) -> dict:
    """单格 inner 窗评估（显式 start/end=只评 inner，探索扫描省一半时长）。"""
    from discovery.objective import evaluate_replay
    res = evaluate_replay(params, universe, split,
                          start=str(split.inner.start), end=str(split.inner.end))
    return res.get("inner") or {}


def run(day: str | None = None, force: bool = False) -> dict:
    from trading.job_ledger import begin_run, finish_run
    day = day or f"{datetime.now():%Y-%m-%d}"
    out = OUT_DIR / f"explore_{day}.json"
    if out.exists() and not force:
        print(f"[explore] {out.name} 已存在（--force 重跑）")
        return json.loads(out.read_text(encoding="utf-8"))

    begin_run(JOB_NAME, day, datetime.now().isoformat())
    t0 = datetime.now()

    def _finish(status: str, msg: str, doc: dict) -> dict:
        doc["day"] = day
        doc["generated_at"] = f"{t0:%Y-%m-%d %H:%M:%S}"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        finish_run(JOB_NAME, day, status, message=msg)
        print(f"[explore] {out.name}（{msg}，{(datetime.now() - t0).total_seconds():.0f}s）")
        return doc

    # ① 输入三源：归因产物 + 冠军基线 + split/universe
    src = OUT_DIR / f"loser_review_{day}.json"
    if not src.exists():
        return _finish("skipped", "当日归因产物缺失（18:05 未跑）", {"skipped": True})
    review = json.loads(src.read_text(encoding="utf-8"))
    from experiment.resolver import resolve_champion
    champ = resolve_champion()
    if champ is None or not champ.params:
        return _finish("skipped", "无 ACTIVE 冠军基线（探索不猜口径）", {"skipped": True})
    champion_params = dict(champ.params)

    grids = plan_grids(review, champion_params)
    if not grids:
        return _finish("skipped", "归因意见无可映射参数维度",
                       {"skipped": True, "champion": champ.experiment_id})

    # ② 扫描：基线 + 各格（inner 窗）
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    split = holdout_split()
    universe, _ = freeze("2025-01-01")
    print(f"[explore] 冠军 {champ.experiment_id}｜{len(grids)} 维网格"
          f"（{sum(len(g['values']) for g in grids)} 格）inner 扫描…")
    base_inner = _evaluate_inner(champion_params, universe, split)
    cells = []
    for g in grids:
        for v in g["values"]:
            params = {**champion_params, g["key"]: v}
            m = _evaluate_inner(params, universe, split)
            cells.append({"key": g["key"], "value": v, "metrics": m})
            print(f"  {g['key']}={v}: n={m.get('n_hits')} "
                  f"胜率{m.get('win_rate', 0):.1%} 年化{m.get('annualized_return', 0):.1%}")

    # ③ 最优格判定：_judge 同款 inner 门槛（胜率+2pp/均rr+0.05/年化+1pp 任一）
    from research.proposals import (MIN_HITS, AVG_RR_IMPROVE, WIN_RATE_IMPROVE,
                                    ANN_IMPROVE)
    best = None
    for c in cells:
        m, b = c["metrics"], base_inner
        if m.get("n_hits", 0) < MIN_HITS:
            continue
        if any([m.get("win_rate", 0) >= b.get("win_rate", 0) + WIN_RATE_IMPROVE,
                m.get("avg_rr", 0) >= b.get("avg_rr", 0) + AVG_RR_IMPROVE,
                m.get("annualized_return", 0) >= b.get("annualized_return", 0) + ANN_IMPROVE]):
            if best is None or (m.get("annualized_return", 0)
                                > best["metrics"].get("annualized_return", 0)):
                best = c

    # ④ 升格：最优格 → 既有提案流（create→verify→publish DRAFT；绝不 promote）
    #    质证工序（2026-09-06）：最优格先过 Tier B 评审——ESCALATE 则放弃该格
    #    （fail-closed 于「进提案库」这步：网格还有别的格/别的天，不亏探索面）
    promoted = None
    if best:
        from research import proposals as P
        base_v = champion_params.get(best["key"])
        hyp = (f"归因驱动探索：{day} 实盘浮亏主因指向 {best['key']}，"
               f"定向扫描 {best['key']}={best['value']}（基线 {base_v}）inner 年化 "
               f"{best['metrics'].get('annualized_return', 0):.1%} vs 基线 "
               f"{base_inner.get('annualized_return', 0):.1%}")
        committee = None
        if os.getenv("COMMITTEE_GATE", "1") != "0":
            try:
                from research.committee.review import review_conclusion
                committee = review_conclusion(
                    f"explore_{day}",
                    f"{hyp}\n最优格 metrics：{json.dumps(best['metrics'], default=str)}"
                    f"\n基线 inner：{json.dumps(base_inner, default=str)}"
                    f"\n来源：当日归因意见→网格扫描后的最优格，待质证后进提案流。",
                    tier="A", timeout_s=900,
                    subject={"params": {best["key"]: best["value"]}})
            except Exception as e:      # noqa: BLE001 —— 评审失败=放行（fail-open）
                committee = {"verdict": "UNAVAILABLE",
                             "notes": f"{type(e).__name__}: {e}"[:200]}
        if committee and committee.get("verdict") == "ESCALATE":
            promoted = {"skipped_by_committee": True,
                        "reason": str(committee.get("notes", ""))[:300],
                        "artifact": committee.get("artifact")}
            print(f"[explore] 最优格被委员会 ESCALATE 放弃（理由进探索报告）："
                  f"{promoted['reason'][:120]}")
        else:
            cnote = "explore_loop" + (
                f"｜committee:{committee.get('verdict')}" if committee else "")
            pid = P.create_proposal(
                P._DEFAULT_DB, change_type="A", hypothesis=hyp[:200],
                params={best["key"]: best["value"]},
                expected_effect=f"定向探索最优格（inner 改善过 _judge 门槛）",
                risk="探索环产物，outer 验证未做前置判断", note=cnote)
            print(f"[explore] 最优格升格提案 {pid} → 既有 verify 流…")
            try:
                ok = P.verify_proposal(P._DEFAULT_DB, pid)
                promoted = {"proposal_id": pid, "verified": ok,
                            "committee": {k: committee.get(k) for k in
                                          ("verdict", "notes")} if committee else None}
                if ok:
                    exp_id = P.publish_proposal(P._DEFAULT_DB, pid)
                    promoted["experiment_id"] = exp_id
                    print(f"[explore] APPROVED → DRAFT {exp_id}（promote 走人审闸，红线不破）")
                else:
                    print(f"[explore] 提案 {pid} 被 verify 拒（理由进学习回路）")
            except P.CommitteeEscalated as e:
                promoted = {"proposal_id": pid, "escalated": str(e)[:300]}
                print(f"[explore] publish 被委员会拦截转人审：{e}")
            except Exception as e:
                promoted = {"proposal_id": pid, "error": f"{type(e).__name__}: {e}"}
                print(f"[explore] 提案验证异常（留 PENDING 人工处置）：{e}")

    return _finish("done", f"{len(grids)} 维 {len(cells)} 格，"
                   f"最优格{'升格 ' + promoted['proposal_id'] if promoted else '无达标'}",
                   {"champion": champ.experiment_id, "grids": grids,
                    "baseline_inner": base_inner, "cells": cells,
                    "best": best, "promoted": promoted,
                    "note": "归因驱动定向探索：意见→网格→inner 扫描→最优格走既有"
                            "提案流；止步 DRAFT，promote 走 autopromote 七门/人审"})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="归因驱动定向参数探索环")
    p.add_argument("--day", default=None, help="业务日 YYYY-MM-DD（缺省今天）")
    p.add_argument("--force", action="store_true", help="当日产物已存在也重跑")
    args = p.parse_args(argv)
    run(day=args.day, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
