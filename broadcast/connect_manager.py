# -*- coding: utf-8 -*-
"""dev connect 对话机器人后台进程管理（Windows · 纯进程管理 · 不依赖 dws/钉钉）。

物理定位：subprocess.Popen 拉起 dws dev connect 常驻 + PID 文件 + 日志 + taskkill /T 树杀。
零钉钉依赖（不 import dws/push/notifier），可独立单测（mock subprocess.Popen / tasklist / taskkill）。

命令组装（spec §4.3）：
- claudecode 类：dws dev connect --unified-app-id <env> --channel claudecode
  --agent-memory --agent-approval-mode ask --allowed-users <env> --agent-workdir <env>
- custom 类（review）：dws dev connect --unified-app-id <env> --channel custom
  --agent-cmd "<agent_cmd>" --allowed-users <env>

安全底线（spec R3 / C2）：approval_mode 永远取 defaults["approval_mode"]="ask"，
本模块不接受 cfg 覆盖——省略 = 钉钉一句话驱动本机 Claude Code 自动改代码/跑高危命令。

cwd 锁根（spec R6 / C4）：start() 在 Popen 时显式传 cwd=PROJECT_ROOT，
化解 start_dingtalk_bots.md「dws cwd 非项目根、相对 agent_cmd 踩坑」教训——
dev connect 继承项目根 cwd，review 的相对 agent_cmd 才能找到 python.exe 与桥脚本。
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 项目根（broadcast/ 的上级 = F:/quanter）：作 Popen cwd，锁 dev connect 工作目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# PID/日志目录（运行时幂等文件，.gitignore 已含 logs/）
RUN_DIR = Path("logs") / "broadcast_connect"

# Windows 进程创建标志：新进程组 + detach（后台独立，不随父 CLI 退出而死）
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def build_cmd(bot: str, cfg: dict, defaults: dict) -> list[str]:
    """组装 dws dev connect 启动命令（claudecode vs custom 两类）。

    cfg:      CONNECT_BOTS[bot]，含 unified_env / channel（+ custom 类的 agent_cmd）。
    defaults: CONNECT_DEFAULTS（allowed_users_env / workdir_env / agent_memory / approval_mode）。

    安全：approval_mode 永远取 defaults["approval_mode"]（="ask"），不接受 cfg 覆盖（C2）。
    身份闸：allowed_users 缺失 → RuntimeError（省略 = 任何钉钉用户都能驱动本机 Claude Code）。
    """
    unified = os.getenv(cfg["unified_env"], "")
    if not unified:
        raise RuntimeError(f"缺环境变量 {cfg['unified_env']}（bot={bot} 的 unified-app-id）")
    allowed = os.getenv(defaults["allowed_users_env"], "")
    if not allowed:
        raise RuntimeError(f"缺身份闸 {defaults['allowed_users_env']}（省略=全放行，高危）")

    cmd: list[str] = [
        "dws", "dev", "connect",
        "--unified-app-id", unified,
        "--channel", cfg["channel"],
    ]
    if cfg["channel"] == "claudecode":
        # claudecode 类：dev connect 自动拉起 Claude Code，走全套 agent 参数
        if defaults.get("agent_memory"):
            cmd.append("--agent-memory")
        # 审批闸：写死 ask（C2），不暴露覆盖口子
        cmd += ["--agent-approval-mode", defaults["approval_mode"]]
        cmd += ["--allowed-users", allowed]
        workdir = os.getenv(defaults["workdir_env"], "")
        if workdir:
            cmd += ["--agent-workdir", workdir]
    elif cfg["channel"] == "custom":
        # custom 类：agent-cmd 喂业务脚本（review 桥）；相对路径靠 Popen cwd 锁根（C4）
        cmd += ["--agent-cmd", cfg["agent_cmd"]]
        cmd += ["--allowed-users", allowed]
    else:
        raise ValueError(f"未知 channel={cfg['channel']}（bot={bot}）")
    return cmd


# ------------------------------------------------------------------ PID 文件

def _pid_file(bot: str) -> Path:
    return RUN_DIR / f"{bot}.pid"


def _log_file(bot: str) -> Path:
    return RUN_DIR / f"{bot}.log"


def _read_pid(bot: str) -> int | None:
    """读 PID；文件不存在/损坏 → None。"""
    try:
        return int(_pid_file(bot).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_pid(bot: str, pid: int) -> None:
    f = _pid_file(bot)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(pid), encoding="utf-8")


def _clear_pid(bot: str) -> None:
    try:
        _pid_file(bot).unlink()
    except FileNotFoundError:
        pass


def _is_alive(pid: int) -> bool:
    """Windows 存活探测：tasklist /FI "PID eq <pid>"。返 True=在跑。

    tasklist 命中 → stdout 含 PID 数字；未命中 → "INFO: No tasks are running ..."。
    tasklist 不在 PATH / 超时 → 视为不存活（保守，让 start 重拉）。
    """
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and str(pid) in (r.stdout or "")


# ------------------------------------------------------------------ 生命周期

def start(bot: str, cfg: dict, defaults: dict) -> str:
    """后台拉起 dev connect（幂等：已跑则跳过）。返 'started' | 'already_running'。

    cwd 锁根（C4）：Popen 传 cwd=PROJECT_ROOT，dev connect 继承 → review 相对 agent_cmd 可用。
    后台 detach：creationflags=CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS，不随父 CLI 退出而死。
    """
    pid = _read_pid(bot)
    if pid is not None and _is_alive(pid):
        return "already_running"
    if pid is not None:
        # PID 文件在但进程已死 → 清死文件（防崩溃留死文件卡死后续 start）
        _clear_pid(bot)

    cmd = build_cmd(bot, cfg, defaults)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    # 日志追加写：dev connect 原始 stdout/stderr，便于 --logs tail 排查
    log_fh = _log_file(bot).open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=PROJECT_ROOT,                                   # C4：锁项目根
        creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        close_fds=True,
    )
    _write_pid(bot, proc.pid)
    logger.info("connect bot=%s 已拉起 pid=%s", bot, proc.pid)
    return "started"


def stop(bot: str) -> str:
    """树杀：taskkill /F /T /PID（C3：/T 连 dev connect 拉起的 Claude Code 子进程一并终止）。

    返 'stopped' | 'not_running'。taskkill 失败/超时仍清 PID 文件（避免死文件卡死后续 start）。
    """
    pid = _read_pid(bot)
    if pid is None:
        return "not_running"
    try:
        # /F 强制 /T 树杀：漏 /T = dev connect 死了但 Claude Code 子进程还活着（孤儿吃资源）
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("taskkill bot=%s pid=%s 异常，仍清 PID 文件", bot, pid, exc_info=True)
    _clear_pid(bot)
    logger.info("connect bot=%s 已停止 pid=%s", bot, pid)
    return "stopped"


def status(bot: str) -> str:
    """单个 bot 状态：'running' | 'dead' | 'not_running'。dead 则清 PID 文件（僵尸清理）。"""
    pid = _read_pid(bot)
    if pid is None:
        return "not_running"
    if _is_alive(pid):
        return "running"
    _clear_pid(bot)   # 僵尸清理：进程已死 → 删死 PID 文件
    return "dead"
