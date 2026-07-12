# -*- coding: utf-8 -*-
"""E2E 残留端口清理（Windows 闭环）。

Why 存在：webapp-testing 的 with_server.py 在 Windows 停服时可能 kill 不掉 vite/uvicorn
子进程，残留进程占 5173-5177/8000 → 下次 `uvicorn server.main:app` 报
`[WinError 10013] 访问套接字权限不允许`（曾两次踩坑）。本脚本在 E2E 跑完
（with_server 退出后）调，清理残留 + socket bind 实测端口可绑（netstat/excludedportrange
都滞后于 winnat 动态保留状态，socket bind 实测最准）。

用法（E2E 命令尾 && 串接，with_server 退出后自动清理）：
    python "$WITH_SERVER" --server ... -- python tests/e2e/caisen_replay_tab.py \
      && python scripts/clean_ports.py

设计（反黑盒 / 极简）：
- 纯标准库，netstat + PowerShell Stop-Process + socket bind，零第三方依赖；
- 只清 5173-5178 + 8000（E2E 用到的端口），不碰其他；
- socket bind 实测兜底 netstat 滞后（10013 根因常是 winnat 动态保留，netstat 查不到）。
"""
import socket
import subprocess
import sys
import time

# E2E 用到的端口（vite 5173-5178 自动递增 + uvicorn 8000）
TARGET_PORTS = [5173, 5174, 5175, 5176, 5177, 5178, 8000]
# socket bind 实测的关键端口（后端 + 前端默认）
VERIFY_PORTS = [8000, 5173]


def _find_listening_pids(port: int) -> list[str]:
    """netstat 找占 port LISTENING 的 PID 列表。"""
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return []
    pids: list[str] = []
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        if f":{port} " in line:
            parts = line.split()
            if parts:
                pids.append(parts[-1])
    return pids


def _kill_pids(pids: list[str]) -> None:
    """PowerShell Stop-Process 强杀（-ErrorAction SilentlyContinue 容忍已退出）。"""
    for pid in set(pids):
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
            check=False,
        )


def _bind_ok(port: int) -> bool:
    """socket bind 实测端口可绑（绕过 netstat/excludedportrange 滞后）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    print("[clean_ports] 清理 E2E 残留端口（vite 5173-5178 + uvicorn 8000）...")
    for port in TARGET_PORTS:
        pids = _find_listening_pids(port)
        if pids:
            print(f"  {port}: kill 残留 PID {pids}")
            _kill_pids(pids)
        # else: 静默（无残留）

    # kill 后等 winnat 释放 + socket bind 实测（netstat 滞后兜底）
    time.sleep(0.8)
    print("[验证] socket bind 实测（绕过 netstat 滞后）：")
    all_ok = True
    for port in VERIFY_PORTS:
        if _bind_ok(port):
            print(f"  {port}: 可绑 ✓")
        else:
            print(f"  {port}: 仍被占 ✗（可能 winnat 动态保留未过期，稍候重试或重启 winnat）")
            all_ok = False
    if all_ok:
        print("[clean_ports] 全部清理 + 验证通过，端口就绪。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
