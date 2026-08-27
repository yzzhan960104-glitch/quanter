# -*- coding: utf-8 -*-
"""掘金终端看门狗（QuanterGmGuard · QMT 退役方案 P0 / G-1）。

物理意图：掘金升格唯一平台后，emgm3 终端是 行情+交易+数据 三合一单点——本脚本
5 分钟一轮做三层探测，降级即钉钉告警（notifier 多通道，本地 alerts.log 兜底）：

  ① 连接层：127.0.0.1:7001 可连（gmterm-serv 活着的前提哨）；
  ② API 层：7002 GET /v3/strategies 带 token（网关+鉴权+策略表三合一心跳）；
  ③ 进程层：交易时段（工作日 09:10~15:40）策略 python 进程应在场。

自愈边界（对齐 2026-08-27 实弹验证的能力面）：
  - 终端/网关挂 → 只告警不拉起（终端启动含登录态，自动拉起是后续 goldminer://
    实验的事），告警文案带人工处置指引；
  - 策略进程缺失但 ①② 健康 → 自动跑 relaunch_strategy.ps1（该链路已实战验证，
    含杀旧幸存者校验）+ INFO 播报；--no-auto-heal 可关。

镜像范本：ops/miniqmt_guard.py（QMT 前任看护，退役后本脚本接棒；语义差异=
QMT 版会拉客户端，掘金版不拉终端）。
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from ops import gm_ops_common as gc
from ops.miniqmt_guard import _notify  # 复用前任的 fire_and_forget 通知封装（同语义）

MARKET_WINDOW = ("09:10", "15:40")   # 进程在场判定的时段（含盘前缓冲与 EOD 后余量）


def _in_market_window(now: datetime | None = None) -> bool:
    n = now or datetime.now()
    if n.weekday() >= 5:
        return False
    hm = n.strftime("%H:%M")
    return MARKET_WINDOW[0] <= hm <= MARKET_WINDOW[1]


def probe_port_7001() -> bool:
    s = socket.socket()
    s.settimeout(2.0)
    try:
        return s.connect_ex(("127.0.0.1", 7001)) == 0
    finally:
        s.close()


def probe_api(cfg: dict) -> tuple[bool, int]:
    """(ok, http_status)。ok=200 且能解析出 data 列表。"""
    status, payload = gc.api_get("/v3/strategies", str(cfg.get("token") or ""))
    return (status == 200 and isinstance(payload, dict)), status


def probe_strategy_process(strategy_dir: Path) -> int:
    """策略 python 进程数（CommandLine 含 <策略目录>\main.py）。

    PS5.1 坑：单命中时管道产物是裸对象（无 Count 属性→空输出），零命中是 $null
    ——必须 @() 强制数组再 .Count，否则单进程会被报成 0（18:21 实测假阴性：
    进程 17576 在场却报 0，晨检若带此 bug 会天天误告警）。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             f"Where-Object {{ $_.CommandLine -like '*{strategy_dir.name}*main.py*' }}).Count"],
            capture_output=True, text=True, timeout=20)
        return int((out.stdout or "").strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return -1                                     # 查询失败≠进程不在，返回 -1 交上层措辞


def auto_heal_strategy(strategy_dir: Path) -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File",
             str(strategy_dir / "relaunch_strategy.ps1")],
            capture_output=True, text=True, timeout=120)
        print(r.stdout, r.stderr)
        return r.returncode == 0
    except subprocess.SubprocessError as e:
        print(f"auto-heal failed: {e!r}")
        return False


def run_once(auto_heal: bool = True) -> dict:
    cfg = gc.runtime_config()
    port_ok = probe_port_7001()
    api_ok, api_status = probe_api(cfg)
    proc_n = probe_strategy_process(gc.GM_STRATEGY_DIR)
    window = _in_market_window()
    healed = False

    problems: list[str] = []
    if not port_ok:
        problems.append("7001 不可连（gmterm-serv/终端疑似未运行）")
    if not api_ok:
        problems.append(f"7002 API 心跳失败（status={api_status}；401=token 失效须换 runtime.json）")
    if window and proc_n == 0:
        problems.append("交易时段策略进程缺失")
    if proc_n > 1:
        problems.append(f"策略进程数={proc_n}（>1 疑似双策略，须人工核查）")

    if auto_heal and window and proc_n == 0 and port_ok and api_ok:
        healed = auto_heal_strategy(gc.GM_STRATEGY_DIR)
        if healed:
            problems = [p for p in problems if p != "交易时段策略进程缺失"]

    st = {"at": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "port_7001": port_ok,
          "api_7002": api_ok, "api_status": api_status, "strategy_procs": proc_n,
          "market_window": window, "auto_healed": healed, "ok": not problems,
          "problems": problems}
    if healed:
        _notify("INFO", f"掘金看护：策略进程缺失已自动 relaunch 恢复（终端/API 健康）")
    if problems:
        _notify("ERROR" if (not port_ok or not api_ok) else "WARN",
                "掘金看护告警：" + "；".join(problems)
                + "。处置：终端问题→人工拉起 emgm3（goldminer-terminal skill）；"
                  "策略问题→检查 relaunch_strategy.ps1 输出。")
    return st


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="掘金终端看门狗（QMT 退役 P0/G-1）")
    p.add_argument("--once", action="store_true", help="跑一轮（默认行为，占位兼容 schtasks）")
    p.add_argument("--no-auto-heal", action="store_true", help="只告警不自动拉起策略")
    p.add_argument("--register", action="store_true", help="注册 QuanterGmGuard schtasks（5 分钟）")
    p.add_argument("--unregister", action="store_true", help="删除 schtasks")
    args = p.parse_args(argv)
    if args.register:
        py = ROOT / ".venv310" / "Scripts" / "python.exe"
        rc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "5",
                             "/TN", "QuanterGmGuard",
                             "/TR", f'"{py}" "{Path(__file__)}"']).returncode
        print("register" if rc == 0 else f"register failed rc={rc}")
        return rc
    if args.unregister:
        rc = subprocess.run(["schtasks", "/Delete", "/TN", "QuanterGmGuard", "/F"]).returncode
        print("unregistered" if rc == 0 else f"unregister rc={rc}")
        return 0
    print(json.dumps(run_once(auto_heal=not args.no_auto_heal), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
