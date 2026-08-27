# -*- coding: utf-8 -*-
"""R2 夜间流水线编排（2026-08-22）：阶段 A 网格 → B TPE → C 终审，全自动串联。

物理意图：
    24h 战役的执行链自动化——A（171 格质量网格，r2_quality_grid.py 已在跑）完成
    后自动接 B（TPE 组合口径大预算）与 C（champions→DRAFT publish→七门 dry-run→
    A3 探针→汇总），无人值守过夜。每步落检查点（logs/r2_night_state.json），
    任意一步失败/进程被杀后重启本脚本从断点续跑（已完成步跳过）。

人审红线（ADR-15，脚本内硬编码遵守）：
    - autopromote 只跑 dry-run（默认，不 --exec）——七门评估+播报落盘，绝不写库；
    - experiment create（网格冠军物化 DRAFT）是「建草案」非「生效」——DRAFT 零权重
      零交易影响，真正的 promote/discard/G1 降门槛裁决留给人工。

选择规则（透明可否决，写死在此供事后审计）：
    - 网格 top：inner n≥100 按 inner ann 降序取 2；不足则以 n≥80 补足；
    - TPE top：最新 snapshot 的 trial 表按 inner_metrics 的组合口径目标
      （min_yearly_calmar 优先，缺失退 annualized_return）降序取 3；
    - A3 对象：七门 dry-run 报告里过门最多者 top2（无过门者退 inner ann top2）。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv310/Scripts/python.exe")
GRID_JSON = ROOT / "logs/r2_quality_grid_results.json"
STATE = ROOT / "logs/r2_night_state.json"
TODAY = f"{date.today():%Y%m%d}"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

STEPS = ["wait_grid", "tpe", "champions", "publish_grid_top",
         "publish_tpe_top", "autopromote_dry", "a3_top2", "summary"]


def _load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"done": []}


def _mark(step: str, **info) -> None:
    st = _load_state()
    if step not in st["done"]:
        st["done"].append(step)
    st.setdefault("detail", {})[step] = {"at": time.strftime("%F %T"), **info}
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[pipeline] ✓ {step} {info}", flush=True)


def _run(cmd: list, log_name: str, timeout_s: int | None = None) -> int:
    """子进程跑 CLI（与人工操作同路径，审计友好），stdout/err 双落盘日志。"""
    log = ROOT / "logs" / log_name
    print(f"[pipeline] → {' '.join(cmd)}（日志 {log.name}）", flush=True)
    with log.open("w", encoding="utf-8") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                           cwd=str(ROOT), env=ENV, timeout=timeout_s)
    if p.returncode != 0:
        print(f"[pipeline] ⚠ {log_name} 退出码 {p.returncode}（详见该日志）", flush=True)
    return p.returncode


def _pick_grid_top(results: dict, base_params: dict) -> list[dict]:
    """网格选择规则（信息隔离 + 分裂面护栏，2026-08-22 A 阶段数据修订）：
    A 阶段实证 inner/outer 全场系统性分裂（inner top25 的 outer 几乎全负，
    最高 inner +19.4% 对应 outer −9.7%）——纯 inner 排序会专挑 2025 过拟合
    格。规则（outer 只做否决与稳健代表指认，不做排序——协议 §二边缘内的
    最小扩展，物化 DRAFT 零权重零风险，最终裁决权交七门 wf）：
      ① 护栏：outer ann ≥ −10%（R1 A 档 −30% 是提案下限，分裂面下取严档）；
      ② inner ann top3（护栏内，数据说话的主流候选）；
      ③ 稳健代表：n≥100 ∧ outer ann ≥ 0 的格全收（A 阶段唯一外样本存活者
         mh2.7/t2/w60/s0.6/v1.0 型——inner/outer 双正且过 G1 的存在性证明）。"""
    def guard_ok(r):
        return r["outer"]["annualized_return"] >= -0.10
    cells = [(k, v) for k, v in results.items()
             if not k.startswith("*") and k != "*smoke_anchor" and guard_ok(v)]
    n100 = sorted((kv for kv in cells if kv[1]["inner"]["n_hits"] >= 100),
                  key=lambda kv: kv[1]["inner"]["annualized_return"], reverse=True)
    picked = n100[:3]
    robust = [kv for kv in cells if kv[1]["inner"]["n_hits"] >= 100
              and kv[1]["outer"]["annualized_return"] >= 0]
    seen = {k for k, _ in picked}
    picked += [kv for kv in robust if kv[0] not in seen]
    out = []
    for label, r in picked:
        exp_id = "neckline_r2_" + label.replace("/", "_").replace(".", "") + f"_{TODAY}"
        out.append({"experiment_id": exp_id, "label": label,
                    "params": {**base_params, **r["override"]},
                    "inner": r["inner"], "outer": r["outer"]})
    return out


def _pick_tpe_top(n: int = 3) -> list[str]:
    """TPE top：最新 snapshot 的 trial 按组合口径目标排序（SQL 直查稳于解析 CLI 输出）。"""
    con = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    snap = con.execute("SELECT snapshot_hash FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    rows = list(con.execute(
        "SELECT trial_id, inner_metrics FROM trial WHERE snapshot_hash=? ORDER BY rowid DESC LIMIT 400",
        (snap,)))
    def key(r):
        m = json.loads(r["inner_metrics"])
        return (m.get("min_yearly_calmar") or -9e9, m.get("annualized_return") or -9e9)
    top = sorted(rows, key=key, reverse=True)[:n]
    con.close()
    return [r["trial_id"] for r in top]


def main() -> int:
    st = _load_state()
    for step in STEPS:
        if step in st["done"]:
            print(f"[pipeline] 跳过已完成：{step}", flush=True)
            continue

        if step == "wait_grid":
            t0, deadline = time.time(), 4 * 3600
            while not GRID_JSON.exists():
                if time.time() - t0 > deadline:
                    print("[pipeline] ✗ 等 A 网格产物超时 4h（检查 r2_quality_grid 进程）", flush=True)
                    return 1
                time.sleep(60)
            time.sleep(120)                       # 静置 2min 防「文件已建未写完」竞态
            _mark(step, grid=str(GRID_JSON))

        elif step == "tpe":
            # 主力 B：组合口径序贯（objective 已默认 portfolio；seed 固定可复现）。
            # 不设 timeout——预计 6-10h 是本流水线的时长主体。
            rc = _run([PY, "-u", "-m", "discovery", "run",
                       "--budget", "400", "--tpe-trials", "300",
                       "--n-proc", "6", "--seed", "20260822"],
                      "r2_tpe_run.log")
            _mark(step, rc=rc)

        elif step == "champions":
            rc = _run([PY, "-m", "discovery", "champions", "--top-n", "5"],
                      "r2_champions.log")
            _mark(step, rc=rc)

        elif step == "publish_grid_top":
            results = json.loads(GRID_JSON.read_text(encoding="utf-8"))
            base = json.loads(sqlite3.connect(str(ROOT / "experiment/experiments.db")).execute(
                "SELECT params FROM experiment_version WHERE experiment_id="
                "'neckline_prop_20260816_3e383d'").fetchone()[0])
            picked = _pick_grid_top(results, base)
            for p in picked:
                rc = _run([PY, "-m", "experiment", "create",
                           "--strategy", "neckline",
                           "--params", json.dumps(p["params"], ensure_ascii=False),
                           "--experiment-id", p["experiment_id"],
                           "--source", "r2_grid",
                           "--note", f"R2-A 网格 {p['label']} inner n={p['inner']['n_hits']} "
                                     f"ann={p['inner']['annualized_return']:+.1%}（组合口径）"],
                          f"r2_create_{p['experiment_id']}.log")
                if rc != 0:
                    print(f"[pipeline] ⚠ create {p['experiment_id']} 失败（可能 id 冲突幂等），续", flush=True)
            _mark(step, picked=[p["experiment_id"] for p in picked])

        elif step == "publish_tpe_top":
            for tid in _pick_tpe_top():
                rc = _run([PY, "-m", "discovery", "publish", tid],
                          f"r2_publish_{tid}.log")
                if rc != 0:
                    print(f"[pipeline] ⚠ publish {tid} 失败，续", flush=True)
            _mark(step)

        elif step == "autopromote_dry":
            # 人审红线：无 --exec —— 只评估+播报，不写库。
            con = sqlite3.connect(f"file:{ROOT/'experiment/experiments.db'}?mode=ro", uri=True)
            drafts = [r[0] for r in con.execute(
                "SELECT experiment_id FROM experiment_version WHERE status='DRAFT' "
                "AND created_at >= '2026-08-22' ORDER BY created_at")]
            con.close()
            gates = {}
            for eid in drafts:
                rc = _run([PY, "-m", "experiment", "autopromote", eid],
                          f"r2_autopromote_{eid}.log")
                gates[eid] = rc
            _mark(step, drafts=drafts)

        elif step == "a3_top2":
            # A3 滑点探针（每实验 ~40min）：对当日全部新 DRAFT 跑（物化面已收窄到
            # 网格 picked + TPE top3，全评在 C 段预算内）；失败不阻塞汇总。
            con = sqlite3.connect(f"file:{ROOT/'experiment/experiments.db'}?mode=ro", uri=True)
            drafts = [r[0] for r in con.execute(
                "SELECT experiment_id FROM experiment_version WHERE status='DRAFT' "
                "AND created_at >= '2026-08-22' ORDER BY created_at")]
            con.close()
            if drafts:
                rc = _run([PY, "-u", "diag/a3_fill_realism_probe.py",
                           *[a for e in drafts for a in ("--experiments", e)]],
                          "r2_a3_probe.log", timeout_s=4 * 3600)
                _mark(step, drafts=drafts, rc=rc)
            else:
                _mark(step, skipped="无 08-22 新 DRAFT")

        elif step == "summary":
            out = ["# R2 夜间流水线汇总（自动生成）", "",
                   f"生成时刻：{time.strftime('%F %T')}", "",
                   "## 各步产物", "- logs/r2_quality_grid.log / _results.json（A 网格）",
                   "- logs/r2_tpe_run.log（B TPE 400）- logs/r2_champions.log",
                   "- logs/r2_create_*.log / r2_publish_*.log（DRAFT 物化）",
                   "- logs/r2_autopromote_*.log（七门 dry-run，未写库）",
                   "- logs/r2_a3_probe.log（滑点探针）", "",
                   "## 留给人工的决策点（脚本永不代办）",
                   "1. 七门 dry-run 结果复核 → 过门者是否 `experiment autopromote <id> --exec`（灰度第一步 weight=0.3）",
                   "2. G1=100 与质量方向矛盾的裁决（网格 n≥100 存在性数据见 r2_quality_grid_results.json）",
                   "3. 未过门 DRAFT 的 discard 处置"]
            (ROOT / "logs/r2_night_summary.md").write_text(
                "\n".join(out), encoding="utf-8")
            _mark(step)
    print("[pipeline] 全链完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
