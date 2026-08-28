# -*- coding: utf-8 -*-
"""gm_ops_common——掘金运维三件套（guard/ingest/morning_check）的共用配置面。

P0 来源：docs/superpowers/plans/2026-08-27-qmt-decommission-gm-sole-source.md（用户
2026-08-27 批准）。三脚本的共同事实：策略目录定位（env 可覆写，缺省=当前实跑腿
d9324346）、runtime.json 三键读取（token 绝不硬编码）、audit/state 路径推导、
7002 API 的带鉴权 GET。集中一处防三份漂移。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# 钉钉凭证在 .env（DINGTALK_*）——schtasks 环境无 .env 自动加载，三件套+日报统一在此
# 读入（miniqmt_guard 前任同款范式；ImportError 容错=无 dotenv 机器仅少推送不炸）。
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

GM_STRATEGY_DIR = Path(os.environ.get(
    "GM_STRATEGY_DIR",
    r"C:\Users\yzzhan\.emgm3\projects\d9324346-9d1b-11f1-ae25-7c10c93fcb7d"))
GM_API_BASE = os.environ.get("GM_API_BASE", "http://127.0.0.1:7002")


def runtime_config(strategy_dir: Path | None = None) -> dict:
    """读策略目录 config/runtime.json（token/strategy_id/account_id）。缺文件抛错。"""
    d = strategy_dir or GM_STRATEGY_DIR
    return json.loads((d / "config" / "runtime.json").read_text(encoding="utf-8"))


def audit_csv_path(day: str, strategy_dir: Path | None = None) -> Path:
    """day='YYYY-MM-DD' → audit/audit_YYYYMMDD.csv 路径（不校验存在）。"""
    d = strategy_dir or GM_STRATEGY_DIR
    return d / "audit" / f"audit_{day.replace('-', '')}.csv"


def state_pkl_path(strategy_dir: Path | None = None) -> Path:
    d = strategy_dir or GM_STRATEGY_DIR
    return d / "state" / "state.pkl"


def api_get(path: str, token: str, timeout: float = 3.0) -> tuple[int, object]:
    """7002 网关带鉴权 GET → (status, parsed_json|None)。异常/非 JSON → (0, None)。

    返回码语义：0=连不上/超时（网络层），200=正常，401=token 失效，其余照传。
    只读 GET，绝无副作用——变更类路由是 skill 纪律禁区（须用户显式指令）。
    """
    req = urllib.request.Request(GM_API_BASE + path,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as e:            # 401/404 等：网关活着，路由/鉴权问题
        return e.code, None
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, None


def notify(level: str, msg: str) -> None:
    """钉钉告警（infra.notifier 多通道 + 本地 alerts.log 兜底；失败软降级不阻断主链）。

    W0（2026-08-28 全库评审 P0-2 修复）：三件套是短命批处理，本函数改【同步发送】——
    build_default_manager() 装配通道（get_default 裸单例零通道零留痕，08-27~08-28 晨检/
    EOD/看护告警整体静默的根因）+ asyncio.run 直发。Why 不用 fire_and_forget：其
    daemon 线程在脚本 return 后被解释器终期化掐断（HTTP 10s 超时窗口内必丢一批）；
    批处理脚本 notify 后无后续逻辑，同步阻塞 3-10s 零代价，消灭整类退出竞态。
    引擎常驻侧（trading/pipeline）仍用 fire_and_forget，互不影响。
    """
    try:
        import asyncio

        from infra.notifier import build_default_manager
        asyncio.run(build_default_manager().notify_risk_event(msg, level))
    except Exception as e:  # 通道自身故障：print 进 schtasks 日志（W0 bat 包装器），不炸主链
        print(f"[notify 降级 print] level={level} msg={msg!r}（通道失败：{e!r}）")
