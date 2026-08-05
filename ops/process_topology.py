# -*- coding: utf-8 -*-
"""引擎进程拓扑探测共享模块（code-review 修复 · 消 trading_supervisor/audit_ssot 重复）。

物理意图：B1 的 trading_supervisor 与 A6 的 audit_ssot 都要探测「端口属主 / pid 文件 /
引擎进程 / miniQMT 客户端」，原实现各复制一份——同一逻辑两处维护必然漂移（如 audit 版
漏了 TRADING_ENGINE_LOCK_DIR 支持）。本模块收口四件套，两处消费方 import 同一实现。

Windows 现实约束（诚实标注）：
  - 端口属主 PID 用 netstat -ano 解析（ops 层允许 subprocess）；
  - 跨进程 cmdline 用 PowerShell Get-CimInstance（短超时），失败降级 Get-Process exe 路径；
  - 任何探测异常返 None/空（宁可漏报不假报），由上层三合一校验再收敛。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv310" / "Scripts" / "python.exe"


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
    """列项目引擎 python 进程（cmdline 含 -m trading / presentation.server.main:app）。

    优先 PowerShell Get-CimInstance（短超时）；失败降级 Get-Process exe 路径匹配
    （venv python 进程视为候选，交由三合一校验再收敛）。诚实降级，不假装精确。
    """
    out: list[dict] = []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' } | "
             "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8)
        raw = json.loads(r.stdout) if r.stdout.strip() else []
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            cmd = item.get("CommandLine") or ""
            if "-m trading" in cmd or "presentation.server.main:app" in cmd:
                out.append({"pid": int(item["ProcessId"]),
                            "exe": item.get("ExecutablePath"), "cmdline": cmd})
    except Exception:
        # 降级：Get-Process 只给 exe 路径；venv python 且是引擎入口的进程视为候选。
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process python* | Select-Object Id,Path | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=8)
            raw = json.loads(r.stdout) if r.stdout.strip() else []
            if isinstance(raw, dict):
                raw = [raw]
            for item in raw:
                exe = str(item.get("Path") or "")
                if exe.lower().startswith(str(VENV_PY).lower()):
                    out.append({"pid": int(item["Id"]), "exe": exe, "cmdline": None})
        except Exception:
            pass
    return out


def client_status() -> dict:
    """miniQMT 客户端进程探测（XtMiniQmt*；count=None=探测失败，0=未起，>1=多实例）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name 'XtMiniQmt*' -ErrorAction SilentlyContinue | "
             "Select-Object -First 1).Id"],
            capture_output=True, text=True, timeout=8)
        pids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        pid = int(pids[0]) if pids and pids[0].isdigit() else None
        return {"running": len(pids) > 0, "pid": pid, "count": len(pids)}
    except Exception:
        return {"running": None, "pid": None, "count": None}
