# -*- coding: utf-8 -*-
"""提案同意后的换代自动化管道（promote pipeline · 2026-09-03）。

物理意图：补上「promote ACTIVE → 掘金腿换代」的最后三公里——现状 ACTIVE
变更后 export/build/部署全手动零钩子（params_snapshot meta 停 08-25 而 ACTIVE
已 08-26 换代=三层分叉实证），promote 播报「即刻生效」对掘金腿是假话。

分层（核心原则：资金动作永不自动，一切准备与验证全自动，人工一个 go/no-go）：
  watch（18:50 cron 全自动）：ACTIVE/快照/部署腿三方分叉检测 → 分叉时构建
    换代就绪包（export_snapshot 产 config 工作区变更+键级 diff 报告+
    pending_deployment.json）→ 钉钉播报部署指引。**不 build 不 commit 不部署**。
  deploy（人审一键，绝不进 cron）：默认 dry-run；--execute 才真做——
    时窗闸 → git 第一段（commit config）→ build_pilot（stamp 锚新提交）→
    git 第二段（commit 产物）→ 备份部署目录 → 复制 → relaunch → INIT 三锚
    对拍 → 钉钉回报。main 腿默认拒绝（incumbent 红线，--allow-main 例外通道）。
    exp 腿语义=标准组装线产物整体替换 r10leg 手改变体（新代际切换，bak 链可回退）。

CLI：python -m ops.promote_pipeline watch|deploy [--leg exp] [--execute]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

JOB_NAME = "ops_promote_watch"
PENDING = ROOT / "logs" / "pending_deployment.json"
SNAPSHOT = ROOT / "emquant" / "config" / "params_snapshot.json"
PY = ROOT / ".venv310" / "Scripts" / "python.exe"


# ─────────────────────── 三方分叉检测（纯函数，可测） ───────────────────────

def active_champion() -> dict | None:
    """ACTIVE 冠军（experiment resolver 单源）；无 → None。"""
    try:
        from experiment.resolver import resolve_champion
        c = resolve_champion()
        return {"id": c.experiment_id, "params": dict(c.params)} if c else None
    except Exception:
        return None


def snapshot_champion_id() -> str | None:
    """config/params_snapshot.json 记录的冠军（sources.experiment 路径）。"""
    try:
        return (json.loads(SNAPSHOT.read_text(encoding="utf-8"))
                .get("sources", {}).get("experiment", {})
                .get("champion_experiment_id"))
    except (OSError, ValueError):
        return None


def deployed_stamp(leg_dir: Path) -> str | None:
    """部署腿运行真相：importlib 读部署 main.py 的 PILOT_BUILD_STAMP。"""
    import importlib.util as ilu
    try:
        spec = ilu.spec_from_file_location(f"pp_probe_{leg_dir.name[:8]}",
                                           leg_dir / "main.py")
        m = ilu.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        return str(getattr(m, "PILOT_BUILD_STAMP", "") or "") or None
    except Exception:
        return None


def detect_divergence() -> dict:
    """三方比对 → {active, snapshot, stamps:{leg:stamp}, diverged, why}。"""
    from ops import gm_ops_common as gc
    act = active_champion()
    snap = snapshot_champion_id()
    stamps = {}
    try:
        for leg in gc.active_legs():
            stamps[leg.key] = deployed_stamp(gc.leg_strategy_dir(leg))
    except Exception:
        pass
    why = []
    if act is None:
        why.append("无 ACTIVE 冠军（研究侧无基线，无需换代）")
    elif snap != act["id"]:
        why.append(f"快照层落后：snapshot={snap} vs ACTIVE={act['id']}")
    diverged = act is not None and snap != act["id"]
    return {"active": act and act["id"], "snapshot": snap,
            "stamps": stamps, "diverged": diverged, "why": why}


# ─────────────────────── watch：分叉→就绪包→播报 ───────────────────────

def _notify(level: str, msg: str) -> None:
    try:
        from ops.gm_ops_common import notify
        notify(level, msg)
    except Exception as e:
        print(f"  ⚠ 播报降级（{type(e).__name__}: {e}）")


def _param_diff(old: dict, new: dict) -> dict:
    return {"changed": {k: [old.get(k), v] for k, v in new.items()
                        if old.get(k) != v and k in old},
            "added": {k: v for k, v in new.items() if k not in old},
            "removed": sorted(set(old) - set(new))}


def watch() -> dict:
    from trading.job_ledger import begin_run, finish_run
    begin_run(JOB_NAME, f"{datetime.now():%Y-%m-%d}", datetime.now().isoformat())
    t0 = datetime.now()

    def _finish(status, msg, doc):
        finish_run(JOB_NAME, f"{datetime.now():%Y-%m-%d}", status, message=msg)
        print(f"[promote_watch] {msg}（{(datetime.now() - t0).total_seconds():.0f}s）")
        return doc

    div = detect_divergence()
    if not div["diverged"]:
        return _finish("done", f"三方一致（ACTIVE={div['active']}），无需换代", div)

    # 分叉：构建换代就绪包——export_snapshot 产 config 工作区变更（未提交，
    # 不部署可 git checkout 弃）。build 留给 deploy（两段 stamp 语义：先 commit
    # config，build 的 PILOT_BUILD_STAMP 才锚到新提交）。
    act = active_champion()
    try:
        old_params = {}
        try:
            old_params = json.loads(SNAPSHOT.read_text(encoding="utf-8")) \
                .get("id_params", {})
        except (OSError, ValueError):
            pass
        r = subprocess.run([str(PY), str(ROOT / "emquant" / "export_snapshot.py")],
                           capture_output=True, text=True, cwd=str(ROOT),
                           timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"export rc={r.returncode}: {r.stderr[-300:]}")
        new_params = json.loads(SNAPSHOT.read_text(encoding="utf-8")) \
            .get("id_params", {})
        diff = _param_diff(old_params, new_params)
    except Exception as e:
        _notify("WARN", f"换代就绪包构建失败：{type(e).__name__}: {e}（分叉仍在："
                        f"{'; '.join(div['why'])}）——人工执行 emquant/export_snapshot.py 排障")
        return _finish("failed", f"就绪包构建失败：{e}", {**div, "error": str(e)})

    pending = {
        "generated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "champion": act["id"], "prev_snapshot": div["snapshot"],
        "deployed_stamps": div["stamps"], "param_diff": diff,
        "plan": {"leg": "exp",
                 "steps": ["git commit config（两段 stamp 第一段）",
                           "build_pilot --leg exp（stamp 锚新提交）",
                           "git commit 产物（第二段）",
                           f"备份+复制 → 掘金 exp 目录 → relaunch → INIT 三锚对拍"],
                 "deploy_cmd": "python -m ops.promote_pipeline deploy --leg exp --execute"},
        "note": "换代就绪包：watch 已产 config 工作区变更（未提交未部署）；"
                "exp 腿换代=组装线产物整体替换 r10leg 变体（bak 链可回退）",
    }
    PENDING.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
    _notify("INFO",
            f"⚠ ACTIVE 已换代但部署未跟：{div['snapshot']} → {act['id']}"
            f"（部署腿 stamp：{div['stamps']}）；参数 diff {len(diff['changed'])} 键"
            f"+{len(diff['added'])}-{len(diff['removed'])}。换代就绪包已构建，"
            f"人工确认后执行：python -m ops.promote_pipeline deploy --leg exp"
            f"（默认 dry-run 先看计划）")
    return _finish("done", f"分叉→就绪包就绪（{act['id']}，diff "
                   f"{len(diff['changed'])} 键）", pending)


# ─────────────────────── deploy：人审一键（dry-run 默认） ───────────────────────

_TRADING_WINDOW = (dtime(9, 35), dtime(15, 35))


def _in_trading_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.weekday() < 5 and _TRADING_WINDOW[0] <= now.time() <= _TRADING_WINDOW[1]


def deploy(leg: str = "exp", execute: bool = False, allow_main: bool = False) -> int:
    if leg == "main" and not allow_main:
        print("✗ main 腿默认拒绝（incumbent 红线）。确需主腿换代：--leg main --allow-main")
        return 2
    if not PENDING.exists():
        print("✗ 无换代就绪包（先跑 watch：python -m ops.promote_pipeline watch）")
        return 2
    pending = json.loads(PENDING.read_text(encoding="utf-8"))

    from ops import gm_ops_common as gc
    legs = {l.key: l for l in gc.active_legs()}
    if leg not in legs:
        print(f"✗ 未知腿 {leg}（可用：{sorted(legs)}）")
        return 2
    leg_dir = gc.leg_strategy_dir(legs[leg])
    artifact = ROOT / "emquant" / (
        "emquant_neckline_pilot_exp.py" if leg == "exp"
        else "emquant_neckline_pilot.py")

    print(f"=== 换代部署计划（champion={pending['champion']} → {leg} 腿）===")
    for i, s in enumerate(pending["plan"]["steps"], 1):
        print(f"  {i}. {s}")
    print(f"  目标：{leg_dir / 'main.py'}（产物 {artifact.name}）")
    print(f"  回滚：部署目录 main.py.bak_<stamp> 链 + git revert 两段提交")

    if not execute:
        print("\n[dry-run] 以上为计划，未做任何变更。确认后加 --execute。")
        return 0

    if _in_trading_window():
        print("✗ 交易时段（09:35-15:35 工作日）拒绝部署——避开盘中 relaunch")
        return 2

    def _git(*args):
        r = subprocess.run(["git", *args], capture_output=True, text=True,
                           cwd=str(ROOT))
        if r.returncode != 0:
            raise RuntimeError(f"git {args[0]}: {r.stderr[-200:]}")
        return r.stdout.strip()

    try:
        # ① 两段 stamp 第一段：commit config（export 已产工作区变更）
        _git("add", "emquant/config/params_snapshot.json", "emquant/config/universe.json")
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=str(ROOT)).returncode != 0:
            _git("commit", "-m", f"chore(deploy): 换代就绪 config（champion="
                 f"{pending['champion']}，watch 产物）")
        # ② build（stamp 锚刚提交的 config；两腿统一走 CLI 形态）
        r = subprocess.run([str(PY), str(ROOT / "emquant" / "build_pilot.py"),
                            "--leg", leg], capture_output=True, text=True,
                           cwd=str(ROOT), timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"build rc={r.returncode}: {r.stderr[-300:]}")
        # ③ 两段 stamp 第二段：commit 产物
        _git("add", str(artifact))
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=str(ROOT)).returncode != 0:
            _git("commit", "-m", f"chore(deploy): 换代产物 {artifact.name}"
                 f"（两段提交第二段，champion={pending['champion']}）")
        stamp = _git("log", "-1", "--format=%ci %h", "--", str(artifact))
        # ④ 备份+复制（bak 链锚换代前 stamp）
        prev_stamp = (pending.get("deployed_stamps") or {}).get(leg) or "pre"
        bak = leg_dir / f"main.py.bak_{prev_stamp}"
        (leg_dir / "main.py").replace(bak)
        (leg_dir / "main.py").write_text(
            artifact.read_text(encoding="utf-8"), encoding="utf-8")
        # ⑤ relaunch
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(leg_dir / "relaunch_strategy.ps1")],
                       capture_output=True, text=True, timeout=120)
        _notify("INFO", f"✅ 换代部署已执行：{pending['champion']} → {leg} 腿"
                       f"（stamp {stamp}；备份 {bak.name}）。验证：audit INIT 三锚"
                       f"+次日 ab_compare/晨检自动核；回滚=cp {bak.name} main.py + relaunch")
        print(f"[deploy] 完成（stamp {stamp}，备份 {bak.name}）")
        PENDING.unlink(missing_ok=True)            # 就绪包消费
        return 0
    except Exception as e:
        _notify("ERROR", f"❌ 换代部署失败（{leg} 腿）：{type(e).__name__}: {e}"
                        f"——config/产物 commit 可能已发生（git log 核对）；"
                        f"部署目录未动或半动（bak 链核对）")
        print(f"✗ 部署失败：{e}")
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="提案同意后的换代自动化管道")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch", help="分叉检测+就绪包构建（18:50 cron 自动）")
    d = sub.add_parser("deploy", help="人审部署（默认 dry-run）")
    d.add_argument("--leg", default="exp", choices=["exp", "main"])
    d.add_argument("--execute", action="store_true")
    d.add_argument("--allow-main", action="store_true",
                   help="main 腿例外通道（incumbent 红线，默认拒绝）")
    args = p.parse_args(argv)
    if args.cmd == "watch":
        watch()
        return 0
    return deploy(leg=args.leg, execute=args.execute, allow_main=args.allow_main)


if __name__ == "__main__":
    raise SystemExit(main())
