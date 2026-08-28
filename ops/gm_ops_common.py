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


# ────────────────────────── 双腿注册表（2026-08-28 双轨方案 §4.2）─────────────────────────
# 物理意图：「主腿(incumbent)/实验腿(challenger)」是三件套/ingest/归档/对照脚本的
# 共同事实源——目录与账户的映射散在任何一处都会漂移（单源纪律）。目录经 env 覆写：
#   - main：GM_STRATEGY_DIR（沿用既有键，已注册 schtasks 命令行零变更）；
#   - exp ：GM_EXP_STRATEGY_DIR / GM_EXP_ACCOUNT_ID（.env，部署实验腿时写入；
#           未设或目录不存在 = 实验腿未启用，消费方静默跳过——未部署期零告警噪音）。

from dataclasses import dataclass


@dataclass(frozen=True)
class LegDef:
    key: str                            # "main"/"exp"（归档子目录名、报告标签源）
    label: str                          # 钉钉/报告文案标签
    strategy_dir_env: str               # 目录覆写 env 键
    account_env: str                    # 账户覆写 env 键
    default_strategy_dir: Path | None   # main=硬缺省；exp=None（须 env 显式给）


_MAIN_LEG = LegDef("main", "主腿", "GM_STRATEGY_DIR", "GM_MAIN_ACCOUNT_ID", GM_STRATEGY_DIR)
_EXP_LEG = LegDef("exp", "实验腿", "GM_EXP_STRATEGY_DIR", "GM_EXP_ACCOUNT_ID", None)
LEGS = (_MAIN_LEG, _EXP_LEG)

# 实验账户（2026-08-28 零 GUI 建成，复现命令见 emquant/tools/gm_sim_account.py）：
# 期初 10 万 / simulate 撮合 / gm-broker-1 通道（与主账户同款）。作 exp 腿 env 未设时
# build_pilot --leg exp 的缺省——与 runtime.json 的 account_id 交叉验证（C1 产物级
# 账户锁的意图层对侧）。
DEFAULT_EXP_ACCOUNT_ID = "c4ba3b2e-a2da-11f1-9262-52560acd7da0"


def leg_strategy_dir(leg: LegDef) -> Path | None:
    """腿的策略目录：env 显式覆写 > 硬缺省；exp 无缺省（未配置即 None）。"""
    v = os.environ.get(leg.strategy_dir_env)
    return Path(v) if v else leg.default_strategy_dir


def active_legs() -> list[LegDef]:
    """在役腿集合。main 恒在（其目录缺失由消费方照常报错——与单腿时代语义一致，
    不因注册表引入「主腿静默消失」的新故障面）；exp 需 env 已设且目录存在。"""
    out = [_MAIN_LEG]
    d = leg_strategy_dir(_EXP_LEG)
    if d is not None and d.exists():
        out.append(_EXP_LEG)
    return out


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
