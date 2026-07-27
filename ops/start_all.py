# -*- coding: utf-8 -*-
"""全栈一键启动（开机自启入口 · 生产/模拟盘常驻）。

与 ops/dev.py 的区别：
  - dev.py：前台开发（uvicorn + vite，Ctrl+C 退出，子进程组清理）
  - start_all.py：后台 detach（uvicorn + connect + engine 各自独立常驻，本脚本跑完即退）
    适合 schtasks ONSTART 或「启动」文件夹开机自启。

启动顺序（依赖链严格，不可调）：
  1. [外部] miniQMT 客户端（GUI 应用，脚本无法启动，仅检测 userdata_mini + 提示）
  2. uvicorn :8000（后端 API；前端 Cockpit + connect review 桥依赖）—— 复用 dev.py 端口清理
  3. connect 5 钉钉机器人（依赖 uvicorn :8000，review 桥 POST /api/v1/training/review）
  4. trading engine（依赖 miniQMT；python -m trading 独立常驻进程）
  5. schtasks 注册（幂等：DataPipeline@17:00 + Brief@18:00 + DiscoveryDaemon@02:00）

进程模型：subprocess.Popen + DETACHED_PROCESS（独立进程组，本脚本退出不杀子进程），
各进程 stdout/stderr 落 logs/<name>.log，便于排查。

开机自启两种方式（见 scripts/start_all.bat 注释）：
  A) 「启动」文件夹（推荐）：放 start_all.bat 快捷方式，登录后自动跑
  B) schtasks ONSTART：开机即跑（需用户密码）
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv310" / "Scripts" / "python.exe"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Windows 后台进程标志：独立进程组 + detach，本脚本退出不杀子进程（常驻关键）
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def _bind_ok(port: int) -> bool:
    """socket bind 实测端口可绑（复用 dev.py 范式，绕过 netstat/winnat 滞后）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_port_busy(port: int, timeout: int = 20) -> bool:
    """等端口被占（服务就绪）—— bind 失败即就绪。轮询 timeout 秒。"""
    for _ in range(timeout):
        if not _bind_ok(port):
            return True
        time.sleep(1)
    return False


def _detached(cmd: list[str], name: str) -> subprocess.Popen:
    """后台 detach 启动常驻进程（独立进程组，stdout 落 logs/<name>.log）。"""
    log = open(LOG_DIR / f"{name}.log", "ab", buffering=0)
    return subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
    )


def _check_miniqmt() -> bool:
    """检测 miniQMT userdata_mini 是否存在（外部 GUI 应用，脚本无法启动，仅提示）。

    Why 仅检测：miniQMT 是 XtMiniQmt.exe GUI 应用，需手动登录（密码不进 .env）。
    userdata_mini 由登录后生成——存在即视为 miniQMT 已启动登录。
    """
    userdata = os.getenv("QMT_USERDATA_PATH", "")
    return bool(userdata) and Path(userdata).exists()


def main() -> int:
    from dotenv import load_dotenv
    # override=True：.env 单一真相源，覆盖系统 env（修：AUTO_TRADE_MODE 被继承 env 压制）
    load_dotenv(ROOT / ".env", override=True)

    print("=" * 60)
    print("Quanter 全栈启动（开机自启 · 后台 detach）")
    print("=" * 60)

    # 1. miniQMT 检测（外部 GUI，无法脚本启动）
    if _check_miniqmt():
        print("[1/5] ✅ miniQMT userdata_mini 就绪")
    else:
        print("[1/5] ⚠️ miniQMT 未启动——engine 将降级（请手动启动 miniQMT 客户端登录）")

    # 2. uvicorn :8000（端口被占=已在跑，跳过；否则后台 detach 启动）
    if not _bind_ok(8000):
        print("[2/5] 端口 8000 已被占（uvicorn 可能已在跑），跳过")
    else:
        print("[2/5] 启动 uvicorn :8000（后端 API）...")
        _detached([str(VENV_PY), "-m", "uvicorn", "presentation.server.main:app",
                   "--host", "127.0.0.1", "--port", "8000"], "uvicorn")
        if _wait_port_busy(8000, timeout=20):
            print("      ✅ uvicorn 就绪（:8000）")
        else:
            print("      ⚠️ uvicorn 20s 未就绪，查 logs/uvicorn.log")

    # 3. connect 5 钉钉机器人（依赖 uvicorn :8000）—— echo y 自动确认（绕过 _read_confirm）
    print("[3/5] 启动 connect 5 钉钉机器人（依赖 uvicorn）...")
    subprocess.run(
        f'echo y| "{VENV_PY}" -m broadcast connect --start all',
        shell=True, cwd=str(ROOT),
    )

    # 4. trading engine（依赖 miniQMT；独立常驻进程）
    print("[4/5] 启动 trading engine（python -m trading，依赖 miniQMT）...")
    _detached([str(VENV_PY), "-m", "trading"], "trading_engine")

    # 5. schtasks 注册（幂等：每次启动重注一遍，清退历史 + 建最新）
    print("[5/5] 注册 schtasks（幂等：DataPipeline + Brief + DiscoveryDaemon）...")
    subprocess.run([str(VENV_PY), str(ROOT / "ops" / "manage_ops_schtasks.py"), "--register"],
                   cwd=str(ROOT))
    subprocess.run([str(VENV_PY), "-m", "discovery.schtasks", "--register"], cwd=str(ROOT))

    print("\n" + "=" * 60)
    print("✅ 全栈启动完成（各进程独立常驻，本脚本退出不影响它们）")
    print("  - uvicorn  :8000          → logs/uvicorn.log")
    print("  - connect 5 bots          → logs/broadcast_connect/<bot>.log")
    print("  - engine（python -m trading）→ logs/trading_engine.log")
    print("  - schtasks: DataPipeline@17:00 / Brief@18:00 / DiscoveryDaemon@02:00")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
