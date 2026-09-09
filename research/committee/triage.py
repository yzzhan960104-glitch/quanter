# -*- coding: utf-8 -*-
"""research/committee/triage.py —— PM 裁决 → 提案流分流执行器。

分流规则（与 explore_loop/提案流红线对齐）：
  - A 档（参数）：PM 给网格（list 值）→ 逐格 inner 窗扫描（冠军基线为底 +
    partial diff——09-04 口径修正同源）→ 最优格 create_proposal → verify_proposal
    （inner/outer 全门槛）→ APPROVED 才 publish_proposal（DRAFT，止步红线不破）；
  - B/C 档（开关/结构）：create_proposal 落 PENDING 人审，不自动验证；
  - 预算护栏：全 A 档合计网格格数 ≤ MAX_CELLS（回测时长可控），超额按
    PM priority 截断并如实记录被裁掉的格。

CLI：python -m research.committee.triage [--pm-file ...] [--dry-run]
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
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

DB = str(ROOT / "logs" / "research_proposals.db")
MAX_CELLS = 12            # 全会战 A 档网格总格数护栏


def parse_pm_verdict(md_path: Path) -> dict:
    """从 PM 裁决 md 提取 ```json 块（兼容裸 JSON 兜底）。"""
    md = md_path.read_text(encoding="utf-8")
    m = re.search(r"```json\s*(\{.*?\})\s*```", md, re.S)
    raw = None
    if m:
        raw = m.group(1)
    else:
        m2 = re.search(r'\{\s*"proposals".*\}\s*$', md, re.S)
        if m2:
            raw = m2.group(0)
    if not raw:
        raise ValueError("PM 裁决中未找到可解析 JSON 块")
    doc = json.loads(raw)
    for p in doc.get("proposals", []):
        p.setdefault("change_type", "C")
        p.setdefault("params", {})
        p["priority"] = int(p.get("priority", 99))
    doc["proposals"].sort(key=lambda p: p["priority"])
    return doc


def _expand_cells(params: dict) -> list[dict]:
    """网格展开：任一键值为 list → 笛卡尔积；否则单格。"""
    keys = [k for k, v in params.items() if isinstance(v, list)]
    if not keys:
        return [dict(params)]
    value_lists = [params[k] if isinstance(params[k], list) else [params[k]]
                   for k in params]
    cells = []
    for combo in itertools.product(*value_lists):
        cells.append({k: v for k, v in zip(params, combo)})
    return cells


def _evaluate_inner(params_partial: dict, baseline_params: dict,
                    universe, split) -> dict:
    from discovery.objective import evaluate_replay
    res = evaluate_replay({**baseline_params, **params_partial}, universe, split,
                          start=str(split.inner.start), end=str(split.inner.end))
    return res.get("inner") or {}


def run(pm_file: Path | None = None, dry_run: bool = False) -> dict:
    from experiment.resolver import resolve_champion
    from discovery.snapshot import freeze
    from discovery.split import holdout_split

    out_dir = pm_file.parent if pm_file else \
        ROOT / "logs" / f"committee_{datetime.now():%Y%m%d}"
    pm_file = pm_file or (out_dir / "pm_verdict.md")
    doc = parse_pm_verdict(pm_file)
    champ = resolve_champion()
    baseline_params = dict(champ.params)
    report: dict = {"pm_file": str(pm_file), "actions": [], "rejected": doc.get("rejected", []),
                    "champion": champ.experiment_id}

    a_props = [p for p in doc["proposals"] if p["change_type"] == "A"]
    bc_props = [p for p in doc["proposals"] if p["change_type"] != "A"]

    # ── B/C 档：直接落 PENDING（人审域） ──
    for p in bc_props:
        if dry_run:
            report["actions"].append({"id": p.get("id"), "type": p["change_type"],
                                      "action": "dry-run:PENDING", "title": p.get("title")})
            continue
        pid = RP.create_proposal(
            DB, change_type=p["change_type"], hypothesis=p.get("mechanism", p.get("title", "")),
            params=None, expected_effect=p.get("expected", ""),
            risk=p.get("risk", ""), note=f"committee20260906:{p.get('title','')}｜"
            f"验证方式:{p.get('validation','')}｜{p.get('id','')}")
        report["actions"].append({"id": p.get("id"), "type": p["change_type"],
                                  "action": f"created:{pid}:PENDING",
                                  "title": p.get("title")})

    # ── A 档：预算内网格 inner 扫描 ──
    budget = MAX_CELLS
    if a_props and not dry_run:
        split = holdout_split()
        universe, _ = freeze("2025-01-01")
        base_inner = _evaluate_inner({}, baseline_params, universe, split)
        report["baseline_inner"] = {k: base_inner.get(k) for k in
                                    ("n_hits", "win_rate", "avg_rr",
                                     "max_drawdown", "annualized_return")}
        print(f"[triage] 基线 inner {report['baseline_inner']}", flush=True)

    for p in a_props:
        cells = _expand_cells(p["params"])
        if len(cells) > budget:
            report["actions"].append({"id": p.get("id"), "type": "A",
                                      "action": f"skipped:网格{len(cells)}格超预算{budget}",
                                      "title": p.get("title")})
            continue
        if dry_run:
            report["actions"].append({"id": p.get("id"), "type": "A",
                                      "action": f"dry-run:{len(cells)}格", "title": p.get("title"),
                                      "cells": cells})
            continue
        split = holdout_split()
        universe, _ = freeze("2025-01-01")
        scored = []
        for c in cells:
            m = _evaluate_inner(c, baseline_params, universe, split)
            scored.append({"cell": c, "inner": {k: m.get(k) for k in
                                                ("n_hits", "win_rate", "avg_rr",
                                                 "max_drawdown", "annualized_return")}})
            print(f"[triage] {p.get('id')} cell {c} → ann "
                  f"{m.get('annualized_return')}", flush=True)
        budget -= len(cells)
        valid = [s for s in scored
                 if (s["inner"].get("n_hits") or 0) >= RP.MIN_HITS]
        best = max(valid or scored,
                   key=lambda s: s["inner"].get("annualized_return") or -9)
        pid = RP.create_proposal(
            DB, change_type="A", hypothesis=p.get("mechanism", p.get("title", "")),
            params=best["cell"], expected_effect=p.get("expected", ""),
            risk=p.get("risk", ""), note=f"committee20260906:{p.get('title','')}｜"
            f"网格{len(cells)}格inner扫描最优｜{p.get('id','')}")
        ok = RP.verify_proposal(DB, pid)
        action = f"created:{pid}:{'APPROVED' if ok else 'REJECTED'}"
        if ok:
            eid = RP.publish_proposal(DB, pid)
            action += f":published:{eid}"
        report["actions"].append({"id": p.get("id"), "type": "A", "action": action,
                                  "title": p.get("title"), "best_cell": best["cell"],
                                  "scan": scored})

    out = out_dir / "triage.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"[triage] 报告 → {out}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pm-file", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pm = Path(args.pm_file) if args.pm_file else None
    run(pm, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
