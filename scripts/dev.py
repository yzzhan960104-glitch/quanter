# -*- coding: utf-8 -*-
"""一键开发启动器（根治 Windows 端口残留/僵尸）。

Why 存在（用户诉求「根本杜绝」反复踩的 10013/僵尸）：
    手动分别起 uvicorn + npm dev 有三个系统性坑——
      ① uvicorn --reload 的 reloader 子进程退出时占着端口不放（僵尸主因）；
      ② Ctrl+C 或关终端没干净 kill 子进程，累积残留 vite/uvicorn；
      ③ 端口被占/被 winnat 动态保留时启动直接失败（10013/10048），无前置检测。
    本启动器三道根治：
      ① 后端不带 --reload（杜绝 reloader 子进程僵尸）；
      ② 启动前 socket bind 实测 8000/5173 空闲，占则调 clean_ports.py 清残留（实测绕过
         netstat/winnat 滞后）；
      ③ uvicorn + vite 注册为子进程组，atexit + SIGINT 双保险干净清理（Ctrl+C 全杀，不残留）。

用法：
    python scripts/dev.py            # 一键起后端 8000 + 前端 5173，Ctrl+C 干净退出
    python scripts/dev.py --reload   # 后端带 --reload（接受偶尔清端口的代价，换热重载）

设计（反黑盒 / 极简）：
- 纯标准库（subprocess/socket/signal/atexit），零新依赖；
- 不带 --reload 是默认（reloader 子进程是 Windows 僵尸主因，根治优先于热重载便利）；
- clean_ports.py 复用（端口检测/清理单一真相源）。
"""
from __future__ import annotations

import atexit
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv310" / "Scripts" / "python.exe"
BACKEND_PORT = 8000
FRONTEND_PORT = 5173

# 子进程组（atexit/SIGINT 统一清理，杜绝僵尸）
_children: list[subprocess.Popen] = []


def _bind_ok(port: int) -> bool:
    """socket bind 实测端口可绑（绕过 netstat/winnat 滞后，最准的就绪判定）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _ensure_port_free(port: int) -> None:
    """端口被占则调 clean_ports.py 清残留（实测，不靠 netstat）。"""
    if _bind_ok(port):
        return
    print(f"[dev] 端口 {port} 被占，调 clean_ports.py 清残留...")
    subprocess.run([str(VENV_PY), str(ROOT / "scripts" / "clean_ports.py")], cwd=ROOT)
    time.sleep(1.0)
    if not _bind_ok(port):
        print(f"[dev] 警告：{port} 清理后仍被占（kernel 僵尸/winnat 保留），"
              f"后端可能起不来。建议重启 Windows 或 netsh 排除该端口。")


def _cleanup() -> None:
    """atexit/SIGINT 统一清理子进程（terminate → kill 兜底，杜绝僵尸）。"""
    for p in _children:
        if p.poll() is None:
            p.terminate()
    for p in _children:
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()


def main() -> int:
    reload = "--reload" in sys.argv
    # 1. 启动前端口检测 + 清残留（根治坑 ② ③）
    _ensure_port_free(BACKEND_PORT)
    _ensure_port_free(FRONTEND_PORT)

    atexit.register(_cleanup)
    # Windows SIGINT（Ctrl+C）→ 干净清子进程（根治坑 ②：Ctrl+C 不残留）
    signal.signal(signal.SIGINT, lambda *_: (_cleanup(), sys.exit(0)))

    # 2. 起 uvicorn（默认不带 --reload，根治坑 ①：reloader 子进程僵尸）
    backend_cmd = [str(VENV_PY), "-m", "uvicorn", "server.main:app",
                   "--port", str(BACKEND_PORT)]
    if reload:
        backend_cmd.append("--reload")
        print("[dev] 后端带 --reload（热重载，但 reloader 子进程可能残留，Ctrl+C 务必干净退出）")
    else:
        print("[dev] 后端不带 --reload（默认，杜绝 reloader 子进程僵尸；改代码手动重启）")
    backend = subprocess.Popen(backend_cmd, cwd=ROOT)
    _children.append(backend)

    # 3. 起 vite（npm run dev，shell=True 兼容 Windows npm.cmd）
    frontend = subprocess.Popen("npm run dev", cwd=ROOT / "web", shell=True)
    _children.append(frontend)

    print(f"[dev] 后端 http://localhost:{BACKEND_PORT} + 前端 http://localhost:{FRONTEND_PORT}")
    print("[dev] Ctrl+C 干净退出（自动清子进程）")

    # 4. 阻塞等后端（后端退出则整体清）
    try:
        backend.wait()
    finally:
        _cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
