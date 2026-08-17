# -*- coding: utf-8 -*-
"""Quanter 引擎原子重启唯一入口（B2 · process-gateway-ssot-final spec §4.1）。

物理意图：08-04/08-05 反复出现「手动 schtasks /Run、手动 python -m trading、系统 Python
直接跑」等多条启动链互踩——本脚本把「停旧树 → 启新」收敛为唯一操作：
  1. 先跑三合一校验（trading_supervisor.status）：不一致 → 拒绝（rc=2），绝不顶掉旧链；
  2. 默认 dry-run：只展示将停止的进程树，不执行任何 taskkill；
  3. --yes 才真正 stop（taskkill /F /T 树杀）→ start（schtasks 优先）。

用法：
  python ops/restart_trading.py status              # 一屏拓扑（同 supervisor --status）
  python ops/restart_trading.py restart             # dry-run：展示将停进程
  python ops/restart_trading.py restart --yes       # 原子：停旧树 → 启新
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from ops import trading_supervisor


def main(argv: list[str] | None = None) -> int:
    # 校验前 load .env（2026-08-17 修复）：supervisor 三合一的 sid 口径 = 显式 >
    # env QMT_SESSION_ID > default——不 load 时 sid 落 default，探测 default.lock
    # （历史残留、空闲）而非引擎真持有的 <sid>.lock → 「端口被监听但锁未持有」
    # 误报拒启（08-17 实弹：123459 链被 default 误报拦重启）。与引擎进程
    # python -m trading 顶部 load_dotenv(override=True) 同口径。
    from dotenv import load_dotenv
    load_dotenv(override=True)

    p = argparse.ArgumentParser(description="Quanter 引擎原子重启（唯一入口）")
    p.add_argument("action", choices=["restart", "status"])
    p.add_argument("--yes", action="store_true",
                   help="真正停旧树并启动；缺省 dry-run 只展示")
    p.add_argument("--port", type=int, default=trading_supervisor.DEFAULT_PORT)
    args = p.parse_args(argv)

    if args.action == "status":
        print(json.dumps(trading_supervisor.status(args.port),
                         ensure_ascii=False, indent=2))
        return 0

    st = trading_supervisor.status(args.port)
    if not st["consistent"]:
        # 三合一不一致 = 旧链/僵持态：拒绝重启，先人工看 status 决定清哪条链。
        # Why 拒绝而非强杀：supervisor 红线「绝不自动 taskkill 未知链」（误杀 schtasks
        # 合法链风险），必须由人确认 drift 后再 --yes 清理。
        print("三合一校验不通过，拒绝重启：", st["drifts"])
        return 2

    trading_supervisor.stop(port=args.port, yes=args.yes)
    if not args.yes:
        print("dry-run：以上为将停止的进程树（无输出=当前无引擎进程）；加 --yes 执行原子重启")
        return 0
    return trading_supervisor.start(port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
