# -*- coding: utf-8 -*-
"""全栈一键启动（开机自启入口 · 生产/模拟盘常驻）。

与 ops/dev.py 的区别：
  - dev.py：前台开发（uvicorn + vite，Ctrl+C 退出，子进程组清理）
  - start_all.py：后台 detach（uvicorn + connect 各自独立常驻，本脚本跑完即退）
    适合 schtasks ONSTART 或「启动」文件夹开机自启。

C-2 scheduling-orchestration Task 9 收口：
  engine + 数据采集 + Brief 已全部收编进 uvicorn :8000 的 lifespan——engine 在
  lifespan 装配块构造 + bootstrap + start，数据采集经 ``pipeline_then_eod`` cron
  事件链驱动（取代原 QuanterDataPipeline/QuanterBrief schtasks）。本脚本不再起
  ``python -m trading`` 独立 engine 进程，也不再注册 DataPipeline/Brief schtasks
  （改用 ``manage_ops_schtasks.py --unregister-pipeline-brief`` 幂等清退历史残留）。

启动顺序（依赖链严格，不可调）：
  1. uvicorn :8000（宿主：engine + 采集 + brief 全在它的 lifespan；前端 Cockpit +
     connect review 桥依赖）—— 复用 dev.py 端口清理
  2. connect 5 钉钉机器人（依赖 uvicorn :8000，review 桥 POST /api/v1/training/review）
  3. schtasks：DiscoveryDaemon@02:00 注册 + 清退已收编的 DataPipeline/Brief

进程模型：subprocess.Popen + DETACHED_PROCESS（独立进程组，本脚本退出不杀子进程），
各进程 stdout/stderr 落 logs/<name>.log，便于排查。

开机自启两种方式（见 scripts/start_all.bat 注释）：
  A) 「启动」文件夹（推荐）：放 start_all.bat 快捷方式，登录后自动跑
  B) schtasks ONSTART：开机即跑（需用户密码）
"""
from __future__ import annotations

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


def main() -> int:
    from dotenv import load_dotenv
    # override=True：.env 单一真相源，覆盖系统 env（修：AUTO_TRADE_MODE 被继承 env 压制）
    load_dotenv(ROOT / ".env", override=True)

    print("=" * 60)
    print("Quanter 全栈启动（engine/采集/Brief 已收进 uvicorn）")
    print("=" * 60)

    # 1. uvicorn :8000（宿主：engine + 采集 + brief 在它的 lifespan）
    # Why 最先：engine 装配在 uvicorn lifespan 内（Task 9），必须先起 uvicorn 才有 engine。
    # 不再单独起 python -m trading（已合并进 lifespan），也不再检测 miniQMT
    # （engine bootstrap 内部对网关连不上做软降级，start_all 不重复检测）。
    if not _bind_ok(8000):
        print("[1/3] 端口 8000 已被占（uvicorn 可能已在跑），跳过")
    else:
        print("[1/3] 启动 uvicorn :8000（宿主：engine + 采集 + brief 在 lifespan）...")
        _detached([str(VENV_PY), "-m", "uvicorn", "presentation.server.main:app",
                   "--host", "127.0.0.1", "--port", "8000"], "uvicorn")
        # timeout=40：uvicorn 冷启动需加载完整数据湖（margin_secs/fund_basic/hs_const 等），
        # 实测就绪 >20s，原 20s 会误报「未就绪」（uvicorn 其实随后正常监听）。40s 留冷启动余量。
        if _wait_port_busy(8000, timeout=40):
            print("      ✅ uvicorn 就绪（:8000）")
        else:
            print("      ⚠️ uvicorn 40s 未就绪，查 logs/uvicorn.log")

    # 2. connect 5 钉钉机器人（独立常驻，依赖 uvicorn :8000）—— echo y 自动确认（绕过 _read_confirm）
    # Why 前台 run 而非 detach：connect 是「短命启动器」（拉起 5 个 connect_manager 托管的
    #   Claude Code 常驻实例后即退），detach 它无常驻意义；且 detach 脱离控制台会让
    #   _read_confirm 的 input() 抛 EOFError → 兜底返 'n' → --start all 被取消、5 机器人全不启。
    # Why 加 timeout：原 run 无超时，connect 死锁（Claude Code 启动卡住）会连累后面的
    #   schtasks 永久阻塞。timeout=120 兜底，超时即由 run 内部 kill+wait 并抛
    #   subprocess.TimeoutExpired（注意：不是 builtins TimeoutError，二者无继承关系，用错会裸崩）。
    print("[2/3] 启动 connect 5 钉钉机器人（依赖 uvicorn）...")
    try:
        subprocess.run(
            f'echo y| "{VENV_PY}" -m broadcast connect --start all',
            shell=True, cwd=str(ROOT), timeout=120,
        )
    except subprocess.TimeoutExpired:
        # 超时已被 run 内部 kill 子进程；start_all 继续推进，不连累 schtasks。
        print("      ⚠️ connect 启动超时(>120s)，已兜底跳过（schtasks 不受阻塞）")

    # 3. schtasks：只注册 DiscoveryDaemon@02:00；清退已收编进 uvicorn 的 DataPipeline/Brief
    # Why 清退：DataPipeline/Brief 的职责已由 engine 的 pipeline_then_eod cron 接管
    #   （在 uvicorn lifespan 内跑），旧 schtasks 残留会与新事件链重复触发/抢资源。
    # --unregister-pipeline-brief 幂等（不存在不报错），每次启动跑一遍防历史环境残留。
    print("[3/3] schtasks（discovery 注册 + 清退 pipeline/brief）...")
    subprocess.run([str(VENV_PY), "-m", "discovery.schtasks", "--register"], cwd=str(ROOT))
    subprocess.run([str(VENV_PY), str(ROOT / "ops" / "manage_ops_schtasks.py"),
                    "--unregister-pipeline-brief"], cwd=str(ROOT))

    print("\n" + "=" * 60)
    print("✅ 完成（engine/采集/Brief 在 uvicorn 内，broadcast/discovery 独立）")
    print("  - uvicorn  :8000（含 engine + 采集 + brief lifespan）→ logs/uvicorn.log")
    print("  - connect 5 bots          → logs/broadcast_connect/<bot>.log")
    print("  - schtasks: DiscoveryDaemon@02:00（DataPipeline/Brief 已清退）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
