# -*- coding: utf-8 -*-
"""QMT session 级单实例锁（防双引擎抢同一 session → connect -1）。

物理意图（[[qmt-connect-1-rootcause]] 教训）：C-5 V1 用 uvicorn 端口 8000 绑定做
天然单例——同端口第二实例 bind 失败即 exit。但 SERVER_PORT 被覆盖（env 注入 /
多实例部署）或直跑 uvicorn 时，第二个引擎仍可能连同一 QMT_SESSION_ID →
xtquant 返 -1（session 占用）→ 全天锁死。本模块以 session 为键加进程级排他锁，
live 模式 bootstrap 连网关前 acquire；拿不到锁 = 已有一实例在跑，直接拒连
（宁缺毋滥，绝不重复连 session）。

实现：Windows ``msvcrt.locking``（LockFile 语义：锁范围随文件、跨句柄/跨进程排他）、
POSIX ``fcntl.flock``。锁文件句柄常驻直到 ``release()``/进程退出（OS 自动释放，
崩溃无残留，无需 PID 探活）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_DIR = "logs"


def _lock_path(session_id: str, lock_dir: Optional[str] = None) -> Path:
    """锁文件路径：``logs/trading_engine_<session>.lock``（env 可覆盖目录）。"""
    d = Path(lock_dir or os.getenv("TRADING_ENGINE_LOCK_DIR") or _DEFAULT_LOCK_DIR)
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or "default"
    return d / f"trading_engine_{safe}.lock"


def _pid_path(session_id: str, lock_dir: Optional[str] = None) -> Path:
    """持有者信息文件：``logs/trading_engine_<session>.pid``（排障可读）。"""
    d = Path(lock_dir or os.getenv("TRADING_ENGINE_LOCK_DIR") or _DEFAULT_LOCK_DIR)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or "default"
    return d / f"trading_engine_{safe}.pid"


class InstanceLock:
    """持有的排他锁；``release()`` 或进程退出时释放（OS 层面自动释放）。"""

    def __init__(self, fd: int, path: Path):
        self._fd = fd
        self.path = path
        self._released = False

    def release(self) -> None:
        """解锁 + 关句柄（幂等；进程退出时 OS 也会自动释放）。"""
        if self._released:
            return
        self._released = True
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            logger.debug("单实例锁释放异常（进程退出时 OS 自动释放）", exc_info=True)
        os.close(self._fd)


def acquire(session_id: str, lock_dir: Optional[str] = None) -> Optional[InstanceLock]:
    """对 session 加排他锁；已被占用返 None，成功返持有句柄。

    Args:
        session_id: QMT_SESSION_ID（锁键；调用方缺省传 "default"）。
        lock_dir: 覆盖锁目录（默认 env TRADING_ENGINE_LOCK_DIR > logs/）。
    """
    path = _lock_path(session_id, lock_dir)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # 锁文件打不开（权限/路径异常）→ 降级放行：宁可连上由网关层报错，也不因
        # 观测通道故障把引擎误杀（与告警通道「尽最大努力」同哲学）。
        logger.exception("单实例锁文件打开失败 %s（降级放行）", path)
        return None
    try:
        # 空文件先写 1 字节（LockFile 锁空区域可能报 ERROR_END_OF_FILE）。
        size = os.lseek(fd, 0, os.SEEK_END)
        if size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        logger.warning("另一实例已持有 QMT session 锁：%s（拒绝重复连接）", path)
        return None
    # pid/时间戳写独立 .pid 文件：锁文件被 LockFile 锁定后其它句柄读不了
    # （含 buffered read 越界），.pid 是排障用可读副本。
    try:
        _pid_path(session_id, lock_dir).write_text(
            f"{os.getpid()} {datetime.now().isoformat()}\n", encoding="utf-8")
    except OSError:
        logger.warning("单实例锁 pid 信息写入失败 %s（不影响锁本身）", _pid_path(session_id, lock_dir))
    logger.info("QMT session 单实例锁已持有：%s（pid=%s）", path, os.getpid())
    return InstanceLock(fd, path)
