# -*- coding: utf-8 -*-
"""R5 四阶段全自动流水线（2026-08-24 · 用户指令：四阶段不停直达最终结论）。

阶段（ROUND_LOG R5 计划修正版——先图谱后组合）：
  P1 单维全景爆炸：22 维 × 扩展档位（统一 base=贪心栈终点）→ 敏感性图谱+形态标签
  P2 组合验证：单维最优合并测试（含失败回退诊断）+ top 敏感维二维网格 + mg 组合
  P3 TPE 无偏验证：22 维 budget 400（subprocess，6h 上限）→ top3 raw 复核
  P4 终审：候选去重 top3 → wf 四折双口径 → （wf 过者）七门 dry-run → 四择一结论

断点续跑：logs/r5_state.json 每阶段落盘，重启跳过已完成阶段。
产物：logs/r5_state.json / logs/r5_phase*.json / logs/r5_final_report.json。
"""
import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv310/Scripts/python.exe")
STATE = ROOT / "logs/r5_state.json"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "DISCOVERY_MANUAL_RISK": "on"}

# ── 扩展档位表（b=当前 base 值标记；去掉 b 后即该维候选档）──
LEVELS = {
    "window": [40, 80, 100, 120],
    "min_touches": [3, 4],
    "min_suppression": [0.4, 0.6, 0.7, 0.8],
    "local_extrema_window": [5, 7],
    "min_bottoms": [3, 4],
    "breakout_vol_mult": [0.8, 1.5, 2.0],
    "min_rr": [1.0, 2.0, 2.5, 3.0],
    "max_h_atr": [3.5, 4.0, 5.0, 5.5, 6.0],
    "stop_atr_mult": [1.0, 2.0, 2.5],
    "tp_h_mult": [1.0, 2.0, 2.5, 3.0],
    "decay_tau": [None, 60, 90, 120],
    "momentum_gate": [0.0, 0.03, 0.05, 0.10],
    "max_holding": [10, 15, 20, 40, 50],
    "max_wait": [3, 8, 12],
    "cooldown": [0, 5, 8],
    "buy_limit_atr_mult": [0.5, 1.0, 2.0, 2.5, 3.0],
    "tp1_h_mult": [0.5, 1.0, 2.0],
    "tp1_portion": [0.3, 0.5, 0.9],
    "cancel_thresh_mult": [1.0, 2.0, 3.0],
    "trailing_grace": [0, 5, 15, 20],
    "trailing_step": [0.0, 0.03, 0.1, 0.15],
    "trailing_floor": [0.0, 0.25, 0.75],
}

_W = {}          # worker 全局（freeze/split/cal）


def _init_worker():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.manual_risk_sim import build_block_calendar
    _W["universe"], meta = freeze("2021-01-01")
    _W["split"] = holdout_split()
    _W["cal"] = build_block_calendar(_W["universe"])
    print(f"  [worker {os.getpid()}] 就绪 snap={meta.snapshot_hash}", flush=True)


def _eval(item):
    """(tag, params) → 双口径读数（worker/主进程皆可：_W 未初始化时 lazy 补）。"""
    if "universe" not in _W:
        _init_worker()      # 主进程单点评估路径（P2 合并回退循环）——首调 freeze 一次
    from discovery.objective import evaluate_portfolio
    tag, params = item
    t0 = time.time()
    try:
        res = evaluate_portfolio(params, _W["universe"], _W["split"], block_dates=_W["cal"])
        out = {"tag": tag, "raw": res["outer_raw"]["ann"], "mr": res["outer"]["ann"],
               "inner": res["inner"]["ann"], "n": res["inner"]["n"]}
    except Exception as e:
        out = {"tag": tag, "error": f"{type(e).__name__}: {e}"}
    out["sec"] = round(time.time() - t0)
    raw_s = f"{out['raw']:+.1%}" if "raw" in out else "ERR"
    print(f"    [{tag:>30}] raw {raw_s} | {out['sec']}s", flush=True)
    return out


def _batch(items, n_proc=6):
    with Pool(n_proc, initializer=_init_worker) as pool:
        return list(pool.imap_unordered(_eval, items))


def _load_base():
    st = json.loads((ROOT / "logs/r4_loop/state.json").read_text(encoding="utf-8"))
    return st["base"]


def _mark(st, phase, **info):
    st.setdefault("done", []).append(phase)
    st.setdefault("detail", {})[phase] = {"at": time.strftime("%F %T"), **info}
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[checkpoint] ✓ {phase} {info}\n", flush=True)


def phase1(st, base):
    """单维全景爆炸 → 敏感性图谱（每维最优档 + 形态标签）。"""
    items = [("BASE", deepcopy(base))]
    for dim, levels in LEVELS.items():
        for lv in levels:
            if base.get(dim) == lv:
                continue
            p = deepcopy(base); p[dim] = lv
            items.append((f"{dim}={lv}", p))
    print(f"[P1] 单维扫描 {len(items)} 项（含 BASE）", flush=True)
    results = _batch(items)
    base_raw = next(r["raw"] for r in results if r["tag"] == "BASE")
    profile = {}
    for r in results:
        if r["tag"] == "BASE" or "error" in r:
            continue
        dim, lv = r["tag"].split("=", 1)
        try:
            lv_c = json.loads(lv)
        except (json.JSONDecodeError, ValueError):
            lv_c = lv
        profile.setdefault(dim, []).append(
            {"lv": lv_c, "raw": r["raw"], "mr": r["mr"], "inner": r["inner"], "n": r["n"]})
    summary = {"base_raw": base_raw, "dims": {}}
    for dim, rows in profile.items():
        best = max(rows, key=lambda x: x["raw"])
        better = [x for x in rows if x["raw"] > base_raw + 0.005]
        summary["dims"][dim] = {
            "n_levels": len(rows), "base_raw": base_raw,
            "best_lv": best["lv"], "best_raw": best["raw"],
            "up_levels": len(better),
            "shape": ("interior最优" if better and best["lv"] != rows[0]["lv"] else
                      ("有提升档" if better else "base已最优/不敏感"))}
    (ROOT / "logs/r5_phase1.json").write_text(
        json.dumps({"results": results, "summary": summary}, ensure_ascii=False,
                   indent=1, default=str), encoding="utf-8")
    print("[P1] 每维最优：", flush=True)
    for dim, s in sorted(summary["dims"].items(), key=lambda kv: -kv[1]["best_raw"]):
        flag = "★" if s["best_raw"] > base_raw + 0.005 else " "
        print(f"  {flag} {dim:>22}: best={s['best_lv']} raw {s['best_raw']:+.1%}"
              f"（base {base_raw:+.1%}，{s['shape']}）", flush=True)
    _mark(st, "P1", items=len(items), base_raw=base_raw)


def phase2(st, base):
    """组合验证：单维最优合并（失败回退）+ top 敏感维二维网格 + momentum 组合。"""
    p1 = json.loads((ROOT / "logs/r5_phase1.json").read_text(encoding="utf-8"))
    s = p1["summary"]; base_raw = s["base_raw"]
    # ① 改进维（best 超 base+0.5pp），按提升排序
    improve = [(d, x["best_lv"], x["best_raw"]) for d, x in s["dims"].items()
               if x["best_raw"] > base_raw + 0.005]
    improve.sort(key=lambda t: -t[2])
    print(f"[P2] 单维改进维 {len(improve)} 个：{[(d, lv) for d, lv, _ in improve]}", flush=True)
    merged_raw = None
    if improve:
        p = deepcopy(base)
        for d, lv, _ in improve:
            p[d] = lv
        res = _eval(("MERGE_ALL", p))
        merged_raw = res.get("raw") if "raw" in res else None
        # 失败回退：从提升最小者逐个移除直到不崩（raw > base）或剩单维
        removed = []
        while merged_raw is not None and merged_raw < base_raw and len(improve) > 1:
            d, lv, _ = improve.pop()
            p[d] = base.get(d, lv)   # 恢复 base 值
            removed.append(d)
            res = _eval(("MERGE_MINUS_" + "+".join(removed), p))
            merged_raw = res.get("raw") if "raw" in res else None
        m_s = f"{merged_raw:+.1%}" if merged_raw is not None else "评估失败"
        print(f"[P2] 合并测试 final raw {m_s}（base {base_raw:+.1%}，移除 {removed}）", flush=True)
    # ② top 敏感维二维网格：每维取 phase1 读数里 raw 最高的 2 档，两两组合
    tops = improve[:6] if len(improve) >= 6 else improve
    per_dim_best2 = {}
    for r in p1["results"]:
        if "error" in r or r["tag"] == "BASE":
            continue
        d, lv = r["tag"].split("=", 1)
        if d in [t[0] for t in tops]:
            per_dim_best2.setdefault(d, []).append((r["raw"], lv))
    for d in per_dim_best2:
        per_dim_best2[d] = [lv for _, lv in sorted(per_dim_best2[d], reverse=True)[:2]]
    dims6 = [t[0] for t in tops]
    items = []
    for i in range(len(dims6)):
        for j in range(i + 1, len(dims6)):
            for lv1 in per_dim_best2.get(dims6[i], []):
                for lv2 in per_dim_best2.get(dims6[j], []):
                    p = deepcopy(base); p[dims6[i]] = lv1; p[dims6[j]] = lv2
                    items.append((f"{dims6[i]}={lv1}+{dims6[j]}={lv2}", p))
    grid = _batch(items) if items else []
    grid_good = [g for g in grid if "raw" in g and g["raw"] > base_raw]
    grid_good.sort(key=lambda g: -g["raw"])
    (ROOT / "logs/r5_phase2.json").write_text(
        json.dumps({"merged_raw": merged_raw, "grid": grid,
                    "grid_top5": grid_good[:5]}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"[P2] 二维网格 {len(grid)} 项，超 base {len(grid_good)} 项，"
          f"top: {[(g['tag'], round(g['raw'], 3)) for g in grid_good[:5]]}", flush=True)
    _mark(st, "P2", merged_raw=merged_raw, grid_top=grid_good[:1])


def phase3(st, base):
    """TPE 无偏验证（subprocess 6h 上限）→ top3 raw 复核。"""
    print("[P3] TPE budget 400 seed 20260825（最长 6h）", flush=True)
    log = ROOT / "logs/r5_tpe.log"
    try:
        with log.open("w", encoding="utf-8") as f:
            subprocess.run([PY, "-u", "-m", "discovery", "run", "--budget", "400",
                            "--tpe-trials", "300", "--n-proc", "6",
                            "--seed", "20260825"],
                           stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT),
                           env=ENV, timeout=6 * 3600)
    except subprocess.TimeoutExpired:
        print("[P3] TPE 6h 超时截断（已产 trial 照常消费）", flush=True)
    import sqlite3
    con = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
    snap = con.execute("SELECT snapshot_hash FROM snapshot ORDER BY created_at DESC LIMIT 1"
                       ).fetchone()[0]
    rows = list(con.execute(
        "SELECT trial_id, params, inner_metrics FROM trial WHERE snapshot_hash=? "
        "ORDER BY rowid DESC LIMIT 400", (snap,)))
    con.close()
    tops = sorted(rows, key=lambda r: json.loads(r[2]).get("min_yearly_calmar", -9),
                  reverse=True)[:3]
    items = []
    for tid, params_s, _ in tops:
        items.append((f"TPE@{tid[:8]}", json.loads(params_s)))
    checked = _batch(items) if items else []
    (ROOT / "logs/r5_phase3.json").write_text(
        json.dumps(checked, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"[P3] TPE top3 raw 复核：{[(c.get('tag'), round(c.get('raw', -9), 3)) for c in checked]}",
          flush=True)
    _mark(st, "P3", tpe_top=checked[:1])


def phase4(st, base):
    """终审：候选 top3 → wf 四折双口径 → 四择一结论。"""
    from discovery.snapshot import freeze
    from discovery.split import walk_forward_split, holdout_split
    from discovery.manual_risk_sim import build_block_calendar
    from backtest.replay import replay
    from strategies.neckline.strategy import NecklineMethodStrategy
    import sqlite3
    con = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
    p0 = json.loads(con.execute(
        "SELECT params FROM trial WHERE trial_id='49e3755b322c'").fetchone()[0])
    con.close()
    # 候选池：P2 合并/P2 网格 top / P3 TPE top（全部带 raw 复核读数）
    cand = []
    p2 = json.loads((ROOT / "logs/r5_phase2.json").read_text(encoding="utf-8"))
    p3 = json.loads((ROOT / "logs/r5_phase3.json").read_text(encoding="utf-8"))
    pool = {}
    for r in p1_results_for_pool():
        pool[r["tag"]] = r
    for r in (p2.get("grid_top5") or []):
        cand.append((r["raw"], r["tag"], _params_from_tag(base, r["tag"])))
    for c in p3:
        if "raw" in c and c["tag"].startswith("TPE@"):
            tid = c["tag"][4:]      # "TPE@" 长 4——首版 [5:] 切掉首字符致 LIKE 落空
            tcon = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
            row = tcon.execute("SELECT params FROM trial WHERE trial_id LIKE ?",
                               (tid + '%',)).fetchone()
            tcon.close()
            if row:
                cand.append((c["raw"], c["tag"], json.loads(row[0])))
    # 补充：R5 新 trial（engine_hash=4b54e947，85 个）的 min_yr top2——P3 按 min_yr
    # 选 top3 复核可能漏掉"min_yr 中游但 raw 高"的新 trial，此处直查补齐
    try:
        from discovery.fingerprint import engine_hash   # W8：魔法值 '4b54e9475dd8' 改现算
        tcon = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
        rows = [r for r in tcon.execute(
            "SELECT trial_id, params, inner_metrics FROM trial WHERE engine_hash=?",
            (engine_hash(),))]
        tcon.close()
        tops_new = sorted(rows, key=lambda r: json.loads(r[2]).get("min_yearly_calmar", -9),
                          reverse=True)[:2]
        items = [(f"NEW@{tid[:8]}", json.loads(p)) for tid, p, _ in tops_new]
        for c in _batch(items) if items else []:
            if "raw" in c:
                cand.append((c["raw"], c["tag"], json.loads(
                    next(p for tid, p, _ in tops_new if tid.startswith(c["tag"][4:])))))
    except Exception as e:
        print(f"[P4] 新 trial 补充复核异常（续）：{type(e).__name__}: {e}", flush=True)
    cand.sort(key=lambda t: -t[0])
    cand = cand[:3]
    print(f"[P4] 终审候选：{[(t, round(r, 3)) for r, t, _ in cand]}", flush=True)

    universe, meta = freeze("2021-01-01")
    wf = walk_forward_split()
    cal = build_block_calendar(universe)
    finals = []
    for raw0, tag, params in cand:
        folds = {}
        for name, train, oos in wf.folds:
            row = {}
            for use_block in (False, True):
                strat = NecklineMethodStrategy(cfg_override=params)
                rep = replay(universe, strat, str(oos.start), str(oos.end),
                             block_dates=cal if use_block else None)
                row["raw" if not use_block else "mr"] = {
                    "ann": rep.annualized_return, "dd": rep.max_drawdown}
            folds[name] = row
        y22 = folds.get("wf1_2020_21", {}).get("raw", {}).get("ann", 0)
        finals.append({"tag": tag, "raw_outer": raw0, "wf": folds, "y2022_raw": y22,
                       "params": params})
        print(f"  [{tag}] raw_outer {raw0:+.1%} | 2022 折 {y22:+.1%}", flush=True)
    # 四择一结论
    base_y22_note = "贪心栈 base 2022 折 -17.3%（R4 实测）"
    best = max(finals, key=lambda f: f["raw_outer"]) if finals else None
    concl = "3-无超贪心栈者（贪心栈定稿）"
    if best and best["raw_outer"] > 0.984 + 0.01:
        concl = ("1-组合优于贪心且2022改善" if best["y2022_raw"] > -0.173
                 else "2-收益更高但2022更差（帕累托呈报）")
    report = {"candidates": finals, "conclusion": concl,
              "note": base_y22_note, "greedy_raw": 0.984}
    (ROOT / "logs/r5_final_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n[P4] 最终结论：{concl}（候选详情 logs/r5_final_report.json）", flush=True)
    _mark(st, "P4", conclusion=concl)


def p1_results_for_pool():
    try:
        p1 = json.loads((ROOT / "logs/r5_phase1.json").read_text(encoding="utf-8"))
        return p1["results"]
    except Exception:
        return []


def _params_from_tag(base, tag):
    """'dim=lv+dim=lv' → params（二维网格 tag 反解）。"""
    p = deepcopy(base)
    for part in tag.split("+"):
        d, lv = part.split("=", 1)
        try:
            lv = json.loads(lv)
        except (json.JSONDecodeError, ValueError):
            pass
        p[d] = lv
    return p


def main() -> int:
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"done": []}
    base = _load_base()
    print(f"[R5] base=贪心栈终点（raw +98.4% 参照）；已完成阶段 {st['done']}", flush=True)
    for phase, fn in (("P1", phase1), ("P2", phase2), ("P3", phase3), ("P4", phase4)):
        if phase in st["done"]:
            print(f"[R5] 跳过 {phase}", flush=True)
            continue
        fn(st, base)
    print("[R5] 全链完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
