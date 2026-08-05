# -*- coding: utf-8 -*-
"""每日播报 CLI 入口（一期观测运营层 · `python -m broadcast`）。

机器人总管（一期）：push 播报类 3 个 + connect 对话类 5 个
  - push   ：trading / data / strategy（一次性 schtasks 触发，本模块 push 主流程）
  - connect：cli / trading_q / data_q / strategy_q / review（dev connect 后台常驻，
             由 connect_manager 管理，Task 4 接入 CLI 子命令路由）

B4（2026-08-05）播报幂等收敛 job_ledger 单口：
  - 旧：`logs/.last_<bot>_brief` 文件（每 bot 独立，同日不重发）
  - 新：job_ledger 行（job_name=`brief_<bot>`，business_date=播报日）
    begin_run/finish_run 成对 + latest_status 查幂等（与 pipeline/pre_open 同源台账）
  - --force 跳过台账检查（强制重推），但推送成功后仍 begin/finish 更新台账为最新 done
  - 跨进程幂等：job_ledger 是 sqlite 共享真相源（取代文件锁）
  - dry_run 不读/写台账（仅打印文案）
  - 周末/节假日 index_daily 最新日不变 → 天然跳过，零废报

降级：dws 推送失败不写台账 done（下次触发重试）；dry_run 不读/写台账。

market 已下线（2026-07-26）：本模块不再含行情播报分支与 build_daily_brief 依赖。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from broadcast import connect_manager
from broadcast.brief_data import build_data_brief
from broadcast.brief_strategy import build_strategy_brief
from broadcast.brief_trading import build_trading_brief
from broadcast.push import push_brief
from config import LAKE_CONFIG
from data.lake_reader import DataLakeReader

logger = logging.getLogger(__name__)

# 实验中心 DB 路径（策略播报单一真相源，2026-08-03 双轨治理）。
# 与 experiment/store._DEFAULT_DB 同源常量；测试可 monkeypatch 本模块属性指向 tmp DB。
_EXPERIMENT_DB = "experiment/experiments.db"

# ===========================================================================
# 机器人总管配置（push 播报类 + connect 对话类）
# ===========================================================================
# 播报类（push）：一次性 → schtasks 到点 / 手工 --force 触发
#   robot_env：对应 .env 中该机器人 dws 应用 robot_code（不同机器人=不同 dws 应用=不同群）
#   title     ：钉钉消息标题前缀，便于一眼区分来源
# B4：删 `last` 键——幂等去重迁 job_ledger（job_name=`brief_<bot>`），不再走文件。
PUSH_BOTS = {
    "trading":  {"robot_env": "TRADING_BOT_ROBOT_CODE",
                 "title": "💰 每日交易播报"},
    "data":     {"robot_env": "DATA_BOT_ROBOT_CODE",
                 "title": "🗄 每日数据播报"},
    "strategy": {"robot_env": "STRATEGY_BOT_ROBOT_CODE",
                 "title": "♟ 每日策略播报"},
}
# market 已下线（2026-07-26）：代码/配置/文档全清，钉钉侧资源由用户移除。
SUPPORTED_BOTS = tuple(PUSH_BOTS.keys())  # ("trading","data","strategy")

# 对话类（connect）：dws dev connect 后台常驻，broadcast connect --start 拉起
#   unified_env：.env 中该机器人 unified-app-id（dev connect 建联用）
#   channel    ：claudecode=对话（dev connect 自动拉 Claude Code）/ custom=业务脚本
#   agent_cmd  ：仅 channel=custom 的 review 有；相对路径靠 connect_manager.Popen cwd 锁根
CONNECT_BOTS = {
    "cli":         {"unified_env": "CLI_BOT_UNIFIED_APP_ID",      "channel": "claudecode"},  # yzzhanCli通用
    "trading_q":   {"unified_env": "TRADING_BOT_UNIFIED_APP_ID",  "channel": "claudecode"},  # quanter交易
    "data_q":      {"unified_env": "DATA_BOT_UNIFIED_APP_ID",     "channel": "claudecode"},  # quanter数据
    "strategy_q":  {"unified_env": "STRATEGY_BOT_UNIFIED_APP_ID", "channel": "claudecode"},  # quanter策略
    "review":      {"unified_env": "REVIEW_BOT_UNIFIED_APP_ID",   "channel": "custom",
                    "agent_cmd": ".venv310/Scripts/python.exe infra/tools/dingtalk_review_bridge.py"},  # yzzhan参数优化
}
# claudecode 类共用的 dev connect 启动参数（DRY，不每 bot 重复）
CONNECT_DEFAULTS = {
    "allowed_users_env": "DINGTALK_ALLOWED_STAFF_IDS",  # 身份闸（.env 已配）
    "workdir_env":       "BROADCAST_AGENT_WORKDIR",     # Claude Code 工作目录（新增=F:/quanter）
    "agent_memory":      True,
    "approval_mode":     "ask",  # 审批闸，写死，绝不为任何 bot 留覆盖口子（C2 安全底线）
}

# 钉钉群组（所有 push 机器人共用一个运营群；机器人身份靠 robot_code 区分）
_GROUP_ID_ENV = "BROADCAST_GROUP_ID"


# 播报只用 index_daily 这 1 个湖（market 下线后 ths_daily/moneyflow/dragon_list 无人用）：
# 仅 _latest_trade_date 需读 index_daily 取最新交易日作默认播报日。trading/data/strategy
# brief 各自走 trading_service/data_service/plans+json，不依赖这三个湖，load 它们纯浪费内存。
_BRIEF_LAKES = ("index_daily",)


def _load_reader() -> DataLakeReader:
    """仅 load 播报用到的 index_daily 湖（收窄到 _BRIEF_LAKES）。

    Why 不全量：LAKE_CONFIG['lakes'] 含 a_shares_daily（9M 行/408MB），trading/data/strategy
    播报都用不到，load 它纯浪费内存+启动时间。parquet 缺失则 lake_reader 内部离线降级（不阻断）。
    """
    reader = DataLakeReader.get_instance()
    lakes = LAKE_CONFIG.get("lakes", {})
    for key in _BRIEF_LAKES:
        path = lakes.get(key)
        if path:
            reader.load(path, key=key)
    return reader


def _latest_trade_date(reader: DataLakeReader) -> str | None:
    """index_daily 最新交易日（YYYY-MM-DD）；湖空/无 date 层级 → None。"""
    df = reader.get_lake("index_daily")
    if df is None or getattr(df, "empty", True):
        return None
    try:
        dates = df.index.get_level_values("date")
    except Exception:
        return None
    try:
        return pd.Timestamp(dates.max()).strftime("%Y-%m-%d")
    except Exception:
        return None


# ===========================================================================
# Brief 构造器路由（trading/data/strategy 注入式取数 + 纯函数渲染）
# ===========================================================================

def _fetch_trading_snapshot(date: str) -> tuple[list, dict | None, list | None, dict]:
    """取交易机器人当日快照四件套：(trades, asset, positions, status)。

    Why 集中取数 + 兜底降级：
    - 网关未连接/取数失败：asset 传 None、positions 传 None，brief 自动降级文案，绝不抛
      （trading 播报是观测层，断线不应阻断播报，而应如实把「断线」播出去）。
    - trades 走同步 query_trades（CSV 全表扫描，本身即降级契约：文件不存在返空列表）。
    - **2026-08-03 修复**：asset/positions/status 不再在本进程自建 QMT 网关
      （独立进程从未 connect → 恒 disconnected 降级；且同 sid 双进程会互斥抢 session），
      改为读运行中 server 的 API（网关所有权唯一归 server/engine）。server 不在/超时
      → 走既有降级文案（观测层绝不阻断播报）。
    """
    # 同步取数：trades 走 query_trades（读 state_store.fill 表，DB-only 真相源）。
    # 历史口径演进：T7 前 query_trades 全表扫 CSV（与 server 同源单文件）；T7 切 fill 表 DB
    # 默认 + env LIVE_TRADE_READ_SOURCE=csv 回退；A2/A4 删 CSV 回退读口（spec §2.4 SSoT），
    # fill 表是成交流水唯一真相源。注释与代码同步，防运维误以为仍走 CSV。
    try:
        from presentation.server.services import trading_service
        trades_payload = trading_service.query_trades(date, date, limit=100)
        trades = list(trades_payload.get("trades", [])) if trades_payload else []
    except Exception:
        logger.warning("query_trades 取数失败，trading brief 成交节降级为空", exc_info=True)
        trades = []

    # 网关态/资金/持仓：读运行中 server 的 API（网关唯一属主 = server 进程）。
    try:
        status = _server_json("/api/v1/trading/status") or {}
    except Exception:
        logger.warning("get_status 取数失败，trading brief 网关态降级为空", exc_info=True)
        status = {}

    asset: dict | None = None
    positions: list | None = None
    try:
        raw_asset = (_server_json("/api/v1/trading/asset", timeout=8.0) or {}).get("asset") or {}
        asset = raw_asset if raw_asset else None
    except Exception:
        logger.warning("取 asset 失败，trading brief 资金节降级", exc_info=True)
        asset = None
    try:
        p = (_server_json("/api/v1/trading/positions", timeout=12.0) or {}).get("positions")
        # broker 明确返 [] = 权威空仓（不回退本地账本）；仅取数失败（异常）
        # 才退回 position_book，避免本地账本滞后把已平仓显示成持仓。
        positions = p if p is not None else _local_positions_fallback()
    except Exception:
        logger.warning("取 positions 失败，trading brief 持仓段退回本地账本", exc_info=True)
        positions = _local_positions_fallback()

    return trades, asset, positions, status


def _server_json(path: str, timeout: float = 5.0):
    """读运行中 server 的 JSON API（播报独立进程不连 QMT，网关属主=server）。

    base 可用环境变量 TRADING_API_BASE 覆盖（默认 http://127.0.0.1:8000）。
    鉴权：QUANTER_API_TOKEN 已配置时自动带 Bearer（server 无鉴权开发态则忽略）。
    任一异常上抛，由调用方降级（观测层绝不阻断播报）。
    """
    import json as _json
    import urllib.request as _request

    base = os.getenv("TRADING_API_BASE", "http://127.0.0.1:8000").rstrip("/")
    req = _request.Request(f"{base}{path}")
    token = os.getenv("QUANTER_API_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _local_positions_fallback() -> list[dict] | None:
    """broker 持仓不可用时的本地账本兜底：{symbol: qty} → [{symbol, qty}]。

    失败返 None（brief 渲染「持仓未知（网关未连接）」，不折叠成空仓），绝不抛——
    观测层断线降级纪律。
    Fix2（用户两轴 review · 注释脱节）：原 docstring「渲染『当前无持仓』」是 T7 前口径，
    T7 后 None 渲染「持仓未知（网关未连接）」以区分「真无仓」与「取数失败」，避免研究员
    把断线误判成空仓而忽略网关故障。注释与渲染逻辑同步。
    """
    try:
        from trading import position_book
        position_book.init_db()
        local = position_book.get_local_positions()
        return [{"symbol": s, "qty": q} for s, q in local.items()] or None
    except Exception:
        logger.warning("读本地持仓失败（brief 持仓段降级为空）", exc_info=True)
        return None


def _fetch_data_snapshot() -> list[dict]:
    """取数据机器人健康度快照：datasets 列表（与 GET /data/datasets 同源）。

    Why 集中取数 + 兜底降级（与 _fetch_trading_snapshot 同纪律）：
    - __main__ 是同步 CLI，data_service.list_datasets() 也是同步（纯读文件系统 + 内存湖，
      无阻塞 IO），直接调用即可——无需 asyncio。
    - 任一异常（DATASET_REGISTRY 未初始化/文件系统异常）均降级为空列表，brief 自动走
      「无数据集」降级文案，绝不抛（数据观测层不应因取数失败而阻断播报）。
    - 补 freshness_hours 字段：list_datasets() 原返字段含 latest_sync 但无实际 lag 小时，
      brief build_data_brief 的「最老 lag」+「异常清单 lag」依赖此字段——这里从 latest_sync
      ISO（UTC）解析为实际 age 小时（None 则不显示 lag，与 missing/failed 语义一致）。
    """
    # 延迟 import：data_service 顶层会触发 config 注册表 + DataLakeReader 耦合，
    # trading/strategy 分支不需要 → 放函数内，避免无谓依赖加载。
    from datetime import datetime, timezone
    from presentation.server.services import data_service

    try:
        raw = data_service.list_datasets() or []
    except Exception:
        logger.warning("list_datasets 取数失败，data brief 降级为空列表", exc_info=True)
        return []

    now_ts = datetime.now(timezone.utc).timestamp()
    out: list[dict] = []
    for item in raw:
        # 只透传 brief 需要的字段（key/status/latest_sync），并按需补 freshness_hours（实际 lag）
        latest_sync = item.get("latest_sync")
        age_hours: float | None = None
        if latest_sync:
            try:
                # list_datasets 写入格式 "%Y-%m-%d %H:%M:%S UTC"（data_service._now_iso 同款）
                ts = datetime.strptime(latest_sync, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                age_hours = max(0.0, (now_ts - ts) / 3600.0)
            except (ValueError, TypeError):
                # latest_sync 格式异常 → 不显示 lag，brief 自动走「无 lag 数据」分支
                age_hours = None
        enriched = {
            "key": item.get("key", "?"),
            "status": item.get("status", "unknown"),
        }
        if age_hours is not None:
            enriched["freshness_hours"] = age_hours
        out.append(enriched)
    return out


def _fetch_data_freshness() -> list:
    """取数据机器人实时性快照：FreshnessResult 列表（双口径播报的第二口径）。

    物理意图（Task5）：健康度只看 parquet mtime（被动，会被「刚重写但内容是旧数据」骗过）；
    本函数主动比对「交易日历期望日 vs 数据湖内容最新日」，回答 T/T-1 数据到没到。

    Why 收口在此（而非 build_data_brief 内部）：
    - 保持 brief_data.build_data_brief 为纯函数（freshness 作为注入式参数，可单测、可降级）。
    - 取数涉及交易日历 + 文件系统读 parquet，属 IO/计算边界，归 __main__ 取数层（与
      _fetch_data_snapshot/_fetch_trading_snapshot 同纪律）。

    Why 只查 ("daily",)：
    - 颈线法核心依赖以 daily 为主（见 data/freshness.py:_KEY_TO_PARQUET 注释），其余湖
      按需在后续检查点②扩展；本期先收 daily 这一根主线。
    - 每次 read_parquet 大文件开销 ~1.75s（455MB），收窄到 daily 单 key 控制单次播报成本。

    兜底降级（数据观测层绝不阻塞播报）：
    - expected_latest_trade_day 异常 → 返空列表（build_data_brief 视 freshness 为空跳过该段）。
    - check_freshness 单 key 异常 → 该 key 不进列表（其余 key 照常，不让单点失败拖垮整段）。
    """
    # 延迟 import：trading.calendar 触发交易日历加载，data.freshness 触发 pandas read_parquet
    # 链路，仅 data 分支需要 → 放函数内，trading/strategy 分支零负担。
    from datetime import datetime

    from data.freshness import check_freshness
    from trading.calendar import expected_latest_trade_day

    try:
        expected = expected_latest_trade_day(datetime.now())
    except Exception:
        # 交易日历异常（极端长假/registry 失效）→ 降级跳过整段实时性，不阻断播报
        logger.warning("expected_latest_trade_day 失败，data brief 实时性段降级跳过",
                       exc_info=True)
        return []

    out: list = []
    for key in ("daily",):
        try:
            out.append(check_freshness(key, expected))
        except Exception:
            # 单 key 失败仅跳过该 key（不让 daily 一个点的异常拖垮整段实时性播报）
            logger.warning("check_freshness(%s, %s) 异常，该 key 跳过实时性段",
                           key, expected, exc_info=True)
    return out


def _fetch_data_ready_signal() -> bool | None:
    """W5 取数据就绪单口判定（state_store.get_ready），供 brief 对账展示。

    物理意图（spec #13 · T10）：brief 的健康度+实时性是「观测口径」（mtime+内容最新日），
    pre_open 挂单决策用 get_ready（内容校验① + pipeline 台账②）。本函数取 get_ready(D)
    注入 brief，让研究员一眼对账「观测 healthy vs 决策 ready」——暴露「播报 healthy、
    挂单拒」漂移。D = expected_latest_trade_day(now)（与 _fetch_data_freshness 同口径）。

    降级纪律（守观测层绝不阻塞）：
        - 交易日历异常 → None（跳过 brief 该段）；
        - get_ready 异常（DB 损坏/表不存在）→ None（跳过，不阻断播报）。
    """
    # 延迟 import（与 _fetch_data_freshness 同）：trading.state_store 触发 DB 链路，
    # 仅 data 分支需要。
    from datetime import datetime

    from trading.calendar import expected_latest_trade_day
    from trading.state_store import get_ready

    try:
        d = expected_latest_trade_day(datetime.now())
    except Exception:
        logger.warning("expected_latest_trade_day 失败，data brief 就绪单口段降级跳过",
                       exc_info=True)
        return None
    try:
        return get_ready(d)
    except Exception:
        # get_ready 内部已 try/except 软降级返 False，理论上不会抛；此处兜底防 DB 路径
        # 解析等模块级异常阻断播报（守「观测层绝不阻塞」纪律）。
        logger.warning("get_ready(%s) 异常，data brief 就绪单口段降级跳过", d,
                       exc_info=True)
        return None


def _fetch_strategy_snapshot(date: str) -> tuple[int | None, dict | None, list]:
    """取策略机器人当日健康度快照三件套：(scan_count, param_iter_state, recent_runs)。

    Why 集中取数 + 兜底降级（与 _fetch_trading_snapshot/_fetch_data_snapshot 同纪律）：
    - 策略观测层**绝不阻塞播报**：任一取数异常均降级，brief 自动走「—」/「无记录」文案。
    - **scan_count 不走 facade.scan**（全市场扫描重且不稳，可能几十秒~分钟级，钉钉播报
      不能挂在这上面）：读 T 日盘后 EOD 落盘的 T+1 计划文件
      ``logs/trading_plans/plan_<trading_day>.json`` 的 ``len(orders)``
      （2026-08-02 修复：旧实现读 ``plans/<date>.json``——该 scan 落盘格式早已停用，
      EOD 现走 trading_plan.save_plan → 恒读不到文件 → 信号数恒「—」）。
      候选链：plan_<next_trading_day(date)>（T 日盘后产出的次日计划）→
      plan_<date>（同日计划，兼容跨日/补跑）→ 旧 plans/<date>.json（{plans: [...]}）。
      文件都不存在（当日未扫描/周末）→ None（brief 降级「—」）。
    - **param_iter_state 单一真相源（B3 2026-08-05 收口）**：仅读 experiment.db ACTIVE
      （``_experiment_active_state``）。无 ACTIVE → None（brief 降级「—」）。**不再回退**
      legacy 冠军治理 JSON——双轨治理收口，单一真相源是 experiment.db，旧文件已停更
      且归档（logs/archive/）。原 legacy 适配逻辑（best_annual=max(ann)、iter=len(tried)）
      随之移除，brief 只渲染 experiment 形态。
    - **recent_runs**（2026-08-03 修复）：优先读当前回测单一真相源
      ``data/replay_tasks.db`` 的 SUCCESS 任务（Spec 1 起 worker 结果落
      report_json，老 JSON 归档已停更——旧实现读 replay_runs/index.json 导致
      「近期回测」恒停在 07-14）；DB 无 SUCCESS 时回退老 JSON 归档。
      进行中任务（PENDING/RUNNING）置顶提示，让播报如实显示「回测已提交但未完成」。
    """
    import json
    from trading.calendar import next_trading_day

    # ── scan_count：读 EOD 落盘计划文件的订单数（零重活） ──
    scan_count: int | None = None
    # T 日盘后 EOD 落盘的是 **T+1（计划生效日）** 计划文件：date 播报日对应
    # plan_<next_trading_day(date)>（如 07-31 周五盘后 → plan_2026-08-03.json）。
    plan_dir = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    try:
        candidates = [next_trading_day(date), date]
    except Exception:
        candidates = [date]
    for cand in candidates:
        try:
            p = plan_dir / f"plan_{cand}.json"
            if not p.exists():
                continue
            with p.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            orders = payload.get("orders") if isinstance(payload, dict) else None
            if isinstance(orders, list):
                scan_count = len(orders)
                break
        except Exception:
            logger.warning("读 %s 失败，scan_count 降级为 None", p, exc_info=True)
            scan_count = None
            break
    # 旧格式兜底：plans/<date>.json（{date, plans: [...], n_plans: N}，已停用但兼容）
    if scan_count is None:
        try:
            plans_path = Path("plans") / f"{date}.json"
            if plans_path.exists():
                with plans_path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                plans_list = payload.get("plans") if isinstance(payload, dict) else None
                if isinstance(plans_list, list):
                    scan_count = len(plans_list)
        except Exception:
            logger.warning("读 plans/%s.json 失败，scan_count 降级为 None", date, exc_info=True)
            scan_count = None

    # ── param_iter_state：单一真相源 experiment.db ACTIVE（B3 2026-08-05 收口）──
    # 双轨治理收口：legacy 冠军治理 JSON 已归档（logs/archive/），不再回退读旧文件。
    # 无 ACTIVE → None（brief 降级「—」），由 _experiment_active_state 单口负责。
    param_iter_state: dict | None = None
    try:
        param_iter_state = _experiment_active_state()
    except Exception:
        # _experiment_active_state 内部已有 try/except 返 None，此处兜底防未预期异常
        logger.warning("取 param_iter_state 异常，降级为 None", exc_info=True)
        param_iter_state = None

    # ── recent_runs：优先读 replay_tasks.db（当前单一真相源） ──
    recent_runs = _recent_runs_from_tasks_db()
    if not recent_runs:
        # 回退：老 JSON 归档（replay_runs/index.json），取最近 5 条摘要
        try:
            idx_path = Path("replay_runs") / "index.json"
            if idx_path.exists():
                with idx_path.open("r", encoding="utf-8") as f:
                    raw_runs = json.load(f)
                if isinstance(raw_runs, list):
                    recent_runs = sorted(
                        raw_runs,
                        key=lambda r: r.get("created_at", "") if isinstance(r, dict) else "",
                        reverse=True,
                    )[:5]
        except Exception:
            logger.warning("读 replay_runs/index.json 失败，recent_runs 降级为空", exc_info=True)
            recent_runs = []

    return scan_count, param_iter_state, recent_runs


def _experiment_active_state() -> dict | None:
    """读 experiment.db 当前 ACTIVE 实验 → {experiment_id, version, best_annual}。

    单一真相源（2026-08-03 双轨治理 → 2026-08-05 B3 收口）：策略播报的「参数迭代状态」
    仅由实验中心提供，与实盘 _eod（resolve_active）同源。best_annual = ACTIVE 版本 note
    里的 outer 去偏年化（discovery publish 写入，如 "outer ann=1.9% ..."），无 note/解析
    失败 → None（brief 只渲染版本号）。无 ACTIVE / DB 缺失 → None（调用方降级「—」，
    **不再回退 legacy JSON**——双轨治理收口）。
    """
    try:
        from experiment.models import ExperimentStatus
        from experiment.store import list_versions
        versions = [
            v for v in list_versions(_EXPERIMENT_DB, status=ExperimentStatus.ACTIVE)
            if v.weight > 0
        ]
        if not versions:
            return None
        # 多 ACTIVE 灰度并存时取权重最大者展示（当前唯一 ACTIVE weight=1.0）
        top = max(versions, key=lambda v: v.weight)
        best_annual = None
        note = top.note or ""
        if "outer ann=" in note:
            try:
                best_annual = float(note.split("outer ann=", 1)[1].split("%", 1)[0]) / 100.0
            except (ValueError, IndexError):
                best_annual = None
        return {
            "experiment_id": top.experiment_id,
            "version": top.version,
            "best_annual": best_annual,
        }
    except Exception:
        logger.warning("读 experiment ACTIVE 失败，param_iter 段降级为 None", exc_info=True)
        return None


def _recent_runs_from_tasks_db() -> list:
    """从 data/replay_tasks.db 读最近回测摘要（SUCCESS 5 条 + 进行中置顶提示）。

    字段映射对齐 build_strategy_brief 期望（run_id/n_hits/win_rate/max_drawdown/
    annualized_return），并透传 created_at 供 brief 计算「距今 N 天」。
    DB 缺失/损坏 → []（调用方回退老 JSON 归档）。
    """
    try:
        from backtest import tasks_db as replay_tasks_db
        replay_tasks_db.init_db()
        tasks = replay_tasks_db.list_tasks(limit=100) or []
    except Exception:
        logger.warning("读 replay_tasks.db 失败，recent_runs 降级为空", exc_info=True)
        return []

    active = [t for t in tasks if t.get("status") in ("PENDING", "RUNNING")]
    done = [t for t in tasks if t.get("status") == "SUCCESS"]
    out: list = []
    # 进行中任务置顶（比已完成更新才提示；旧 PENDING 卡死由调度器 sweep 兜底）
    if active and (not done or active[0].get("created_at", "") > done[0].get("created_at", "")):
        out.append({
            "run_id": "回测中",
            "pending": True,
            "created_at": active[0].get("created_at", ""),
            "n_hits": -1,
        })
    for t in done[:5]:
        rep = t.get("report") or {}
        dd = rep.get("max_drawdown")
        if dd is not None:
            try:
                if not (-1.0 < float(dd) <= 0.0):
                    # 2026-08-05：legacy 报告 max_drawdown 是累计 rr 口径（如 -412.62），
                    # 不是净值百分比——从 equity_curve 重算，避免播报渲染 -41262%。
                    dd = _recompute_drawdown_from_curve(rep.get("equity_curve"))
            except (TypeError, ValueError):
                dd = None
        out.append({
            "run_id": (t.get("created_at") or "")[:10].replace("-", ""),
            "created_at": t.get("created_at", ""),
            "start": t.get("start") or "",
            "end": t.get("end") or "",
            "n_hits": rep.get("n_hits") or 0,
            "win_rate": rep.get("win_rate") or 0.0,
            "max_drawdown": dd,
            "annualized_return": rep.get("annualized_return") or 0.0,
        })
    return out


def _recompute_drawdown_from_curve(curve) -> float | None:
    """legacy 报告回撤重算：净值曲线 peak-to-trough（equity_0=1.0），返负值；无曲线返 None。"""
    if not curve:
        return None
    peak = 1.0
    dd = 0.0
    for p in curve:
        try:
            eq = float((p or {}).get("equity") or 1.0)
        except (TypeError, ValueError):
            continue
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1.0)
    return dd


def _build_brief(bot: str, date: str, reader: DataLakeReader):
    """按 push 机器人路由到对应 brief 构造器（注入式取数 + 纯函数渲染）。

    market 已下线；trading/data/strategy 各自取数失败均降级，不阻断播报。
    本函数集中路由，避免 main() 里散落 if/elif。
    """
    if bot == "trading":
        # 交易机器人：取数注入 → 纯函数渲染。取数失败任一项均降级，不阻断播报。
        trades, asset, positions, status = _fetch_trading_snapshot(date)
        return build_trading_brief(
            date, trades=trades, asset=asset, positions=positions, status=status,
        )
    if bot == "data":
        # 数据机器人：取 datasets 快照 → 纯函数渲染健康度文案。取数失败降级为空列表。
        # Task5 并入实时性口径：双口径播报（mtime 健康度 + 内容最新日实时性），
        # 主动比对交易日历期望日 vs 数据湖内容最新日，回答「T/T-1 数据到没到」。
        # W5（spec #13 · T10）：补 get_ready 单口判定，让研究员对账「观测 healthy vs 决策 ready」
        # ——若 healthy 但 ready=False 即暴露「播报 healthy、挂单拒」漂移。get_ready 异常降级为
        # None（跳过该段，不阻断播报；守观测层绝不阻塞纪律）。
        datasets = _fetch_data_snapshot()
        freshness = _fetch_data_freshness()
        ready_signal = _fetch_data_ready_signal()
        return build_data_brief(date, datasets=datasets, freshness=freshness,
                                ready_signal=ready_signal)
    if bot == "strategy":
        # 策略机器人：取信号数/参数迭代/近期回测三件套 → 纯函数渲染健康度文案。
        # scan_count 不走 facade.scan（全市场扫描太重），改读 plans/<date>.json。
        scan_count, param_iter_state, recent_runs = _fetch_strategy_snapshot(date)
        return build_strategy_brief(
            date,
            scan_count=scan_count,
            param_iter_state=param_iter_state,
            recent_runs=recent_runs,
        )
    # 兜底：CLI argparse choices 已挡，这里是第二道防线
    raise ValueError(f"未知 bot={bot}，支持：{SUPPORTED_BOTS}")


# ===========================================================================
# CLI 主入口
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    """CLI 总入口（机器人总管）。返回 0=成功/跳过，1=无法定播报日，2=推送失败。

    子命令路由（C1 兼容红线）：
      - 首参为 'connect' → _main_connect（对话机器人后台托管）
      - 首参为 'push'    → _main_push（显式等价默认）
      - 其余（含 --bot/无参）→ _main_push（schtasks 'python -m broadcast --bot trading' 零改动）
    """
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] == "connect":
        return _main_connect(raw[1:])
    if raw and raw[0] == "push":
        return _main_push(raw[1:])
    return _main_push(raw)


def _main_push(argv: list[str]) -> int:
    """push 子命令（播报）：生成文案 → push_brief → 退出。无子命令默认即此路径。

    返回 0=成功/跳过，1=无法定播报日，2=推送失败。

    B4 幂等迁 job_ledger（取代旧 .last_<bot>_brief 文件）：
      - job_name = `brief_<bot>`，business_date = 播报日
      - 推送前查 latest_status：已 done 且非 --force → 跳过
      - 推送成功 → begin_run(INSERT OR REPLACE) + finish_run(UPDATE done) 成对落台账
      - --force 跳过台账检查（强制重推），但仍 begin/finish 更新台账为最新 done
      - finish_run 只 UPDATE——必须先 begin_run INSERT，否则 0 行受影响（台账无记录→幂等失效）

    Why job_ledger 取代文件：
      - 跨进程幂等：sqlite 共享真相源（多进程读 latest_status 一致），取代文件锁竞态
      - 与 pipeline/pre_open 同源台账（C-8 范式统一），减少幂等机制种类
      - SSoT 收口：spec §A2 .last_<bot>_brief 与 job_ledger 双幂等机制合并
    """
    from trading import job_ledger
    from trading.clock import now as _now  # started_at 用统一时钟源（C-6 单口）

    p = argparse.ArgumentParser(
        prog="python -m broadcast", description="钉钉播报（push 类 · 一次性）"
    )
    p.add_argument("--bot", default="trading", choices=SUPPORTED_BOTS, help="push 机器人身份")
    p.add_argument("--date", help="播报日 YYYY-MM-DD（缺省=index_daily 最新交易日）")
    p.add_argument("--dry-run", action="store_true", help="只打印文案不发钉钉")
    p.add_argument("--force", action="store_true", help="忽略幂等去重强制重发")
    args = p.parse_args(argv)

    reader = _load_reader()
    date = args.date or _latest_trade_date(reader)
    if date is None:
        logger.error("无法确定播报日（index_daily 未加载/为空）；用 --date 显式指定")
        return 1

    # B4 幂等查台账：job_name=`brief_<bot>`，已 done 且非 --force → 跳过
    # dry_run 不查台账（仅打印文案，不消费幂等额度）
    job_name = f"brief_{args.bot}"
    if not args.dry_run and not args.force:
        try:
            if job_ledger.latest_status(job_name, date) == "done":
                print(f"{args.bot} 今日({date})已播报，跳过（--force 可重发）")
                return 0
        except Exception:
            # 台账读异常（DB 损坏/路径解析）→ 软降级：warning 但继续推送
            # （观测层纪律：台账故障不应阻断播报；最坏情况重复推送一次，比漏推可控）
            logger.warning("查 brief 台账失败 date=%s（继续推送，幂等降级）", date,
                           exc_info=True)

    brief = _build_brief(args.bot, date, reader)
    title = f"{PUSH_BOTS[args.bot]['title']} {date}"
    robot_code = os.getenv(PUSH_BOTS[args.bot]["robot_env"], "")
    group_id = os.getenv(_GROUP_ID_ENV, "")
    ok = push_brief(
        title, brief.markdown,
        robot_code=robot_code, group_id=group_id, dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0
    if ok:
        # 推送成功 → begin/finish 成对落台账（finish 只 UPDATE，须先 begin INSERT）
        # --force 重推路径也走这里：把台账更新为最新 done（覆盖之前的 done 终态）
        try:
            job_ledger.begin_run(job_name, date, started_at=_now().isoformat())
            job_ledger.finish_run(job_name, date, "done")
        except Exception:
            # 台账写异常 → warning，不影响本次推送结果（下次触发可能重复推，可接受）
            logger.warning("写 brief 台账失败 date=%s（不影响本次推送，下次可能重复）",
                           date, exc_info=True)
        print(f"{args.bot} 播报已推送({date})")
        return 0
    logger.error("%s 推送失败，未写台账 done（下次触发重试）", args.bot)
    return 2


def _read_confirm() -> str:
    """读二次确认输入（y/N）。无 tty（schtasks/管道）→ EOFError 兜底返 'n'（保守不启）。"""
    try:
        return input("确认？[y/N] ").strip().lower()
    except EOFError:
        return "n"


def _main_connect(argv: list[str]) -> int:
    """connect 子命令：dev connect 对话机器人后台托管（start/stop/status/logs）。

    生命周期托管给 connect_manager（PID 文件 + 日志 + 树杀 + 僵尸清理）。
    """
    p = argparse.ArgumentParser(
        prog="python -m broadcast connect",
        description="对话机器人后台托管（connect 类 · dev connect 常驻）",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", metavar="BOT|all", help="拉起 connect 机器人（bot key 或 all）")
    g.add_argument("--stop", metavar="BOT|all", help="停止（bot key 或 all）")
    g.add_argument("--status", action="store_true", help="全部 connect bot 状态 + 僵尸清理")
    g.add_argument("--logs", metavar="BOT", help="查日志（tail 最后 40 行）")
    args = p.parse_args(argv)

    if args.status:
        for bot in CONNECT_BOTS:
            print(f"{bot}: {connect_manager.status(bot)}")
        return 0
    if args.start:
        return _connect_start(args.start)
    if args.stop:
        return _connect_stop(args.stop)
    if args.logs:
        return _connect_logs(args.logs)
    return 0


def _connect_start(target: str) -> int:
    """拉起 connect 机器人。target='all' 需二次确认（防误启 5 个 Claude Code 实例）。"""
    bots = list(CONNECT_BOTS) if target == "all" else [target]
    for b in bots:
        if b not in CONNECT_BOTS:
            print(f"未知 connect bot={b}，支持：{list(CONNECT_BOTS)}")
            return 1
    if target == "all":
        print(f"即将拉起 {len(bots)} 个 connect 机器人：{bots}（= {len(bots)} 个 Claude Code 常驻实例）")
        if _read_confirm() != "y":
            print("已取消")
            return 0
    for b in bots:
        try:
            res = connect_manager.start(b, CONNECT_BOTS[b], CONNECT_DEFAULTS)
        except RuntimeError as e:
            # 缺 unified-app-id / 身份闸 → 该 bot 跳过，不让单点阻断其余
            print(f"{b}: 配置缺失跳过（{e}）")
            continue
        print(f"{b}: {res}")
    return 0


def _connect_stop(target: str) -> int:
    """停止 connect 机器人（树杀）。target='all' 批量停。"""
    bots = list(CONNECT_BOTS) if target == "all" else [target]
    for b in bots:
        if b not in CONNECT_BOTS:
            print(f"未知 connect bot={b}，支持：{list(CONNECT_BOTS)}")
            return 1
        print(f"{b}: {connect_manager.stop(b)}")
    return 0


def _connect_logs(bot: str) -> int:
    """tail 某 connect bot 日志最后 40 行（dev connect 原始输出）。"""
    if bot not in CONNECT_BOTS:
        print(f"未知 connect bot={bot}，支持：{list(CONNECT_BOTS)}")
        return 1
    log_path = connect_manager._log_file(bot)
    if not log_path.exists():
        print(f"无日志（{bot} 未启动过）：{log_path}")
        return 0
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-40:]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
