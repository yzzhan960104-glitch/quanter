# -*- coding: utf-8 -*-
"""Quanter 引擎进程超级管理器（B1 · 进程单一所有者）。

物理意图（process-gateway-ssot-final spec §4.1）：08-04/08-05 反复出现「新/旧进程并存、
系统 Python 占生产端口、三条启动链互踩」——根因是没有单一所有者。本模块是三合一校验的
唯一实现：**端口 8000 属主 PID == pid 文件 PID == session 锁持有者**，三者一致才允许
启动；不一致只告警/拒绝，绝不自动 taskkill（误杀 schtasks 合法链风险）。

Windows 现实约束（诚实标注）：
  - 端口属主 PID 用 netstat -ano 解析（ops 层允许 subprocess；进程内仍用 socket 探测）；
  - 跨进程 cmdline 用 PowerShell Get-CimInstance（短超时），失败降级 Get-Process exe 路径；
  - 锁持有探测会短暂 acquire 再 release，先备份 pid 文件内容恢复，避免污染三合一判定。

用法：
  python ops/trading_supervisor.py --status          # 一屏拓扑 + 三合一一致性
  python ops/trading_supervisor.py --start           # 校验通过后拉起（schtasks 优先）
  python ops/trading_supervisor.py --stop --yes      # 停引擎进程树（缺省 dry-run）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 脚本直接运行时 sys.path[0]=ops/，需锚定项目根（与 ops/manage_ops_schtasks.py 同范式）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
VENV_PY = ROOT / ".venv310" / "Scripts" / "python.exe"
DEFAULT_PORT = 8000

# Windows DETACHED 标志（与 presentation/server/main.py discovery 子进程同范式）：
# CREATE_NEW_PROCESS_GROUP(0x200) → 独立进程组；DETACHED_PROCESS(0x8) → 无控制台。
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008

# code-review 修复：端口/进程/客户端探测抽到 ops/process_topology.py 共享
# （trading_supervisor 与 audit_ssot 同源，防两处漂移）。
from ops.process_topology import (
    client_status as _client_status,
    engine_processes,
    pid_file_owner,
    port_holder_pid,
    session_id as _session_id,
)


def lock_held(session_id: str | None = None, lock_dir: str | None = None) -> bool:
    """探测 session 锁是否被持有（不污染 pid 文件）。

    Why 先备份再 acquire：single_instance.acquire 在锁空闲时会重写 pid 文件，
    探测不能把 supervisor 的 pid 写进引擎 pid 文件（否则三合一校验失真）。
    探测异常保守视为已持有（fail-closed：宁可拒绝启动也不放行第二个引擎）。
    """
    from trading import single_instance
    sid = _session_id(session_id)
    path = single_instance._pid_path(sid, lock_dir)
    backup: str | None = None
    try:
        backup = path.read_text(encoding="utf-8") if path.exists() else None
        lock = single_instance.acquire(sid, lock_dir)
    except Exception:
        return True
    if lock is None:
        return True
    try:
        if backup:
            path.write_text(backup, encoding="utf-8")
        elif path.exists():
            path.unlink()
    finally:
        lock.release()
    return False


def _git_rev() -> str | None:
    """当前 HEAD 短哈希（P0-3；失败返 None，不阻断）。"""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _read_runtime_session() -> int | None:
    """runtime SSoT 实际 sid（B2 L2 轮换落地后由 broker 写入 logs/engine_session.json）。"""
    try:
        p = ROOT / "logs" / "engine_session.json"
        if p.exists():
            return int(json.loads(p.read_text(encoding="utf-8")).get("session_id"))
    except Exception:
        pass
    return None


def _runtime_started_at() -> str | None:
    """引擎启动时间（B2 前从 pid 文件时间戳近似；None=无记录）。"""
    try:
        from trading.single_instance import _pid_path
        p = _pid_path(_session_id())
        if p.exists():
            return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        pass
    return None


def status(port: int = DEFAULT_PORT, session_id: str | None = None) -> dict:
    """三合一进程拓扑一屏视图。

    consistent=True 仅当：端口属主与 pid 文件一致（都 None 也视为一致=未启动），
    且锁态不出现「端口无监听但锁被持有」的僵持。
    """
    owner = port_holder_pid(port)
    pidf = pid_file_owner(session_id)
    held = lock_held(session_id)
    drifts: list[str] = []
    if owner is not None and pidf is not None and owner != pidf:
        drifts.append(f"端口属主 {owner} != pid 文件 {pidf}")
    if owner is None and pidf is not None:
        drifts.append("pid 文件存在但端口无监听（进程已死/未绑定）")
    if owner is None and held:
        drifts.append("session 锁被持有但端口无监听（异常/僵持态）")
    if owner is not None and not held:
        drifts.append(f"端口被 {owner} 监听但 session 锁未持有（旧链/非法链）")
    return {
        "port": port,
        "port_holder_pid": owner,
        "pid_file_pid": pidf,
        "lock_held": held,
        "engine_pids": [p["pid"] for p in engine_processes()],
        "preferred_sid": _session_id(session_id),
        "actual_sid": _read_runtime_session(),
        "client": _client_status(),
        "git": _git_rev(),
        "started_at": _runtime_started_at(),
        "consistent": not drifts,
        "drifts": drifts,
    }


def start(port: int = DEFAULT_PORT) -> int:
    """校验通过后拉起引擎（schtasks 优先，降级直接 detach venv python -m trading）。"""
    st = status(port)
    if not st["consistent"]:
        print("三合一校验不通过，拒绝启动：", st["drifts"])
        return 2
    if st["engine_pids"]:
        print("引擎已在运行 pids=", st["engine_pids"])
        return 0
    rc = subprocess.run(["schtasks", "/Run", "/TN", "QuanterServer"],
                        capture_output=True, text=True, timeout=15).returncode
    if rc == 0:
        print("已通过 schtasks /Run QuanterServer 拉起")
        return 0
    log_path = ROOT / "logs" / "quanter_supervisor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        subprocess.Popen([str(VENV_PY), "-m", "trading"], cwd=str(ROOT),
                         stdout=fh, stderr=subprocess.STDOUT,
                         creationflags=_CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS)
    print("schtasks 不可用，已直接拉起 venv python -m trading（日志 logs/quanter_supervisor.log）")
    return 0


def stop(port: int = DEFAULT_PORT, yes: bool = False) -> int:
    """停引擎进程树；缺省 dry-run 只展示（B1 红线：不自动 taskkill）。"""
    procs = engine_processes()
    if not procs:
        print("无引擎进程可停")
        return 0
    pids = [p["pid"] for p in procs]
    print("将停止引擎进程树：", pids)
    if not yes:
        print("dry-run：加 --yes 才执行 taskkill /F /T")
        return 0
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=30)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Quanter 引擎进程超级管理器（三合一校验）")
    # code-review 修复：--status 为无参默认（plan 契约），start/stop 才需显式。
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("--status", action="store_true", help="一屏拓扑 + 三合一一致性（默认）")
    g.add_argument("--start", action="store_true", help="校验通过后拉起引擎")
    g.add_argument("--stop", action="store_true", help="停引擎进程树（需 --yes 执行）")
    p.add_argument("--yes", action="store_true", help="--stop 时真正执行 taskkill")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--session", default=None, help="QMT_SESSION_ID 覆盖（缺省 env）")
    args = p.parse_args(argv)
    if not (args.status or args.start or args.stop):
        args.status = True
    if args.status:
        print(json.dumps(status(args.port, args.session), ensure_ascii=False, indent=2))
        return 0
    if args.start:
        return start(args.port)
    if args.stop:
        return stop(args.port, yes=args.yes)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
