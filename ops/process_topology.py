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


def default_port() -> int:
    """引擎 API 端口单一来源（code-review：端口 8000 硬编码漂移修复）。

    读 SERVER_PORT env（与 trading.__main__.run_server 同口径），缺省 8000；
    guard/supervisor/audit 全部从这里取，不再各自写死 8000。
    """
    try:
        return int(os.environ.get("SERVER_PORT", "8000"))
    except (TypeError, ValueError):
        return 8000


def session_id(session_id: str | None = None) -> str:
    """锁/pid 文件键：显式 > env QMT_SESSION_ID > default（与 single_instance 同口径）。"""
    return session_id or os.environ.get("QMT_SESSION_ID", "default")


def port_holder_pid(port: int | None = None) -> int | None:
    """netstat -ano 解析 :port LISTENING 的 PID；无监听/解析失败返 None。"""
    port = default_port() if port is None else port
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


def _pid_alive(pid: int) -> bool:
    """PID 是否存活（Get-Process 探测；异常保守视为 False）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue | Measure-Object).Count"],
            capture_output=True, text=True, errors="replace", timeout=8)
        return (r.stdout or "").strip() == "1"
    except Exception:
        return False


def _drop_engine_descendants(
    matched: list[dict], ppid_of: dict[int, int]
) -> list[dict]:
    """递归祖先链去重：matched 中任一祖先是引擎 pid 的进程全部丢弃，只留进程树根。

    为什么不能只看直接父（T10 实证 2026-08-15 进程快照）：引擎树中间可能夹
    「cmdline 不含 -m trading 的中间进程」——multiprocessing spawn worker 的
    cmdline 是 spawn_main、.bat 经 cmd.exe 包装——其再拉起的 -m trading 子进程
    直接父不在匹配集合内，一级 ppid 判据漏网（audit 误计多引擎 / stop 树杀漏孙辈）。
    ppid_of 须传**全进程表** pid→ppid 映射（含非 python 中间进程），沿祖先链逐级
    回溯，命中任一引擎 pid 即判后代。seen 集 防 PID 复用造成的祖先环（回溯不死循环）。
    """
    engine_pids = {p["pid"] for p in matched}

    def _has_engine_ancestor(pid: int) -> bool:
        seen: set[int] = set()
        cur = ppid_of.get(pid)
        while cur and cur not in seen:
            if cur in engine_pids:
                return True
            seen.add(cur)
            cur = ppid_of.get(cur)
        return False

    return [p for p in matched if not _has_engine_ancestor(p["pid"])]


def engine_processes() -> list[dict]:
    """列项目引擎 python 进程根（cmdline 含 -m trading / presentation.server.main:app）。

    优先 PowerShell Get-CimInstance（短超时）；失败降级 Get-Process exe 路径匹配
    已移除——08-06 实测会误把 connect bot/数据同步等所有 venv python 都算成引擎
    （stop 会误杀）。降级改为「端口属主 + pid 文件属主」两个引擎锚点，绝不扩大匹配。
    去重（T10 实证升级 · 2026-08-15 快照）：Windows venv 启动器会拉起 base 解释器
    子进程（cmdline 都含 -m trading），且 base 解释器还会经 multiprocessing spawn
    worker（cmdline 不含 -m trading）挂孙辈——旧「直接父 ∈ 引擎集合」一级判据对
    「经非匹配中间进程挂下来的孙辈」漏网。现取全进程表 pid→ppid 映射做**递归祖先
    链去重**（_drop_engine_descendants），全树只留根：stop() 用 taskkill /T 树杀时
    根+子+孙一起清，audit 计数也回到「引擎数=1」；两条独立树（真·双起抢 sid）则
    都保留——探测不掩盖双起事实。
    """
    matched: list[dict] = []
    ppid_of: dict[int, int] = {}
    try:
        # Why 取全表（不再 Where-Object 过滤 python）：祖先链回溯要经过非 python 中间
        # 进程（spawn worker 也是 python 但 cmdline 不匹配；cmd.exe/.bat 包装则非 python），
        # 只取 python 子集会在中间进程处断链。Name/CommandLine 过滤移到 Python 侧做。
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Select-Object Name,ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, errors="replace", timeout=8)
        raw = json.loads(r.stdout) if r.stdout.strip() else []
        if r.returncode != 0 or not raw:
            # PowerShell/CIM 失败（rc≠0 或空输出）→ 走 except 降级锚点，不返回假空。
            raise RuntimeError(f"CIM unavailable rc={r.returncode}")
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            try:
                pid = int(item["ProcessId"])
                ppid = int(item.get("ParentProcessId") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            ppid_of[pid] = ppid  # 全表 pid→ppid（祖先链回溯的图，含非 python 进程）
            name = item.get("Name") or ""
            cmd = item.get("CommandLine") or ""
            # 与旧 PowerShell 侧 Where-Object Name -match '^python' 同口径（防 bat/ps
            # 命令行里恰含 -m trading 的非 python 进程误入匹配集）。
            if not name.lower().startswith("python"):
                continue
            if "-m trading" in cmd or "presentation.server.main:app" in cmd:
                matched.append({"pid": pid, "ppid": ppid,
                                "exe": item.get("ExecutablePath"), "cmdline": cmd})
    except Exception:
        # 降级：PowerShell/CIM 不可用时，用「端口属主 + pid 文件属主」两个引擎锚点——
        # 宁可漏报（返回空）也不把 bot/数据同步误判成引擎（stop 误杀风险高于漏报）。
        # 只保留存活锚点：陈旧 pid 文件（进程已死）不算引擎，否则 start 误判「已在运行」。
        anchors = {x for x in (port_holder_pid(), pid_file_owner()) if x and _pid_alive(x)}
        return [{"pid": p, "ppid": 0, "exe": None, "cmdline": None} for p in anchors]
    return _drop_engine_descendants(matched, ppid_of)


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
