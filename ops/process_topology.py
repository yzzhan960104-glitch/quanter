# -*- coding: utf-8 -*-
"""引擎进程拓扑探测共享模块（code-review 修复 · 消 trading_supervisor/audit_ssot 重复）。

物理意图：B1 的 trading_supervisor 与 A6 的 audit_ssot 都要探测「端口属主 / pid 文件 /
引擎进程 / miniQMT 客户端」，原实现各复制一份——同一逻辑两处维护必然漂移（如 audit 版
漏了 TRADING_ENGINE_LOCK_DIR 支持）。本模块收口四件套，两处消费方 import 同一实现。

Windows 现实约束（诚实标注）：
  - 端口属主 PID 用 netstat -ano 解析（ops 层允许 subprocess）；
  - 跨进程 cmdline 用 PowerShell Get-CimInstance（短超时），失败降级为端口/pid 文件锚点；
  - 任何探测异常返 None/空（宁可漏报不假报），由上层三合一校验再收敛。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def session_id(session_id: str | None = None) -> str:
    """锁/pid 文件键：显式 > env QMT_SESSION_ID > default（与 single_instance 同口径）。"""
    return session_id or os.environ.get("QMT_SESSION_ID", "default")


def port_holder_pid(port: int = 8000) -> int | None:
    """netstat -ano 解析 :port LISTENING 的 PID；无监听/解析失败返 None。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, errors="replace", timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "LISTENING" in line and f":{port} " in line:
            parts = line.split()
            try:
                return int(parts[-1])
            except (IndexError, ValueError):
                return None
    return None


def pid_file_owner(sid: str | None = None, lock_dir: str | None = None) -> int | None:
    """logs/trading_engine_<sid>.pid 首字段（引擎持有者 PID；尊重 TRADING_ENGINE_LOCK_DIR）。"""
    from trading.single_instance import _pid_path
    try:
        # Why 参数名用 sid 而非 session_id：避免遮蔽本模块的 session_id() 解析函数
        # （遮蔽会导致 session_id(session_id) 把字符串当函数调用 → 探测恒 None）。
        text = _pid_path(session_id(sid), lock_dir).read_text(encoding="utf-8")
        return int(text.split()[0])
    except Exception:
        return None


def engine_processes() -> list[dict]:
    """列项目引擎 python 进程根（cmdline 含 -m trading / presentation.server.main:app）。

    优先 PowerShell Get-CimInstance（短超时）；失败降级 Get-Process exe 路径匹配
    已移除——08-06 实测会误把 connect bot/数据同步等所有 venv python 都算成引擎
    （stop 会误杀）。降级改为「端口属主 + pid 文件属主」两个引擎锚点，绝不扩大匹配。
    去重：Windows venv 启动器会拉起 base 解释器子进程（cmdline 都含 -m trading），
    同一引擎会匹配出 2 个 pid——按 ParentProcessId 过滤，只保留进程树根（launcher），
    stop() 用 taskkill /T 树杀时根+子一起清，audit 计数也回到「引擎数=1」。
    """
    out: list[dict] = []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' } | "
             "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, errors="replace", timeout=8)
        raw = json.loads(r.stdout) if r.stdout.strip() else []
        if r.returncode != 0 or not raw:
            # PowerShell/CIM 失败（rc≠0 或空输出）→ 走 except 降级锚点，不返回假空。
            raise RuntimeError(f"CIM unavailable rc={r.returncode}")
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            cmd = item.get("CommandLine") or ""
            if "-m trading" in cmd or "presentation.server.main:app" in cmd:
                out.append({"pid": int(item["ProcessId"]),
                            "ppid": int(item.get("ParentProcessId") or 0),
                            "exe": item.get("ExecutablePath"), "cmdline": cmd})
    except Exception:
        # 降级：PowerShell/CIM 不可用时，用「端口属主 + pid 文件属主」两个引擎锚点——
        # 宁可漏报（返回空）也不把 bot/数据同步误判成引擎（stop 误杀风险高于漏报）。
        anchors = {x for x in (port_holder_pid(8000), pid_file_owner()) if x}
        return [{"pid": p, "ppid": 0, "exe": None, "cmdline": None} for p in anchors]
    # 去重：子进程的 ppid 落在引擎集合内 → 只留树根（launcher）。
    pids = {p["pid"] for p in out}
    out = [p for p in out if p.get("ppid") not in pids]
    return out


def client_status() -> dict:
    """miniQMT 客户端进程探测（XtMiniQmt*；count=None=探测失败，0=未起，>1=多实例）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name 'XtMiniQmt*' -ErrorAction SilentlyContinue | "
             "Select-Object -First 1).Id"],
            capture_output=True, text=True, errors="replace", timeout=8)
        if r.returncode != 0:
            raise RuntimeError(f"client probe rc={r.returncode}")
        pids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        pid = int(pids[0]) if pids and pids[0].isdigit() else None
        return {"running": len(pids) > 0, "pid": pid, "count": len(pids)}
    except Exception:
        return {"running": None, "pid": None, "count": None}
