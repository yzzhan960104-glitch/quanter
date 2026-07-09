# -*- coding: utf-8 -*-
"""Celery 实例 + 蔡森形态学流水线 beat 三任务（Phase 3 · Task 5）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"自动调度层"——挂载三个 @celery_app.task，
    实现"T 日收盘扫描 + 盘中每 60s 回踩/持仓监控"全自动执行：

        caisen.scan_universe       T 日 15:30  调 caisen_service.run_scan 全市场扫描
        caisen.monitor_pullback    60.0s       盘中调 ExecutionEngine.tick_pullback（ARMED→FILLED）
        caisen.monitor_holding     60.0s       盘中调 ExecutionEngine.tick_exit（FILLED→CLOSED）

历史背景：
    原 run_factor_grid / run_factor_grid_impl 因强依赖 factors.analyzer /
    factors.exploratory_momentum，已在 Phase 1·Task 3 随 factors 体系整体删除。
    本模块的 Celery app 单例 + task_default_queue 配置保留，Phase 3·Task 5 在此
    重新挂载蔡森 beat 三任务（不再回引因子框架）。

关键工程取舍（Why）：
- Celery app 为模块级单例，实例化仅记录 broker_url，不在此刻连 Redis（lazy）；
  开发机/CI 无 Redis 时仍可正常 import 本模块——`.delay()` 时才显式抛
  redis.ConnectionError（由调用方降级，绝不阻断主流程）。
- 监控任务双闸门（断线保护 + 交易时段挡板）：非交易时段 / 网关非 live 直接 return，
  不进入编排链路（隔夜/周末 beat 空转；断线不补发，等下一轮重连）。
- async 包裹：tick_pullback / tick_exit 是 async 方法，Celery 同步任务内用
  asyncio.run() 包裹驱动事件循环（prefork worker 默认同步执行模型）。

防御性边界（CLAUDE.md 量化风控·边界审查 · 拷问三连）：
    - 流动性与极端行情：beat 周期 60s 远大于 A 股 Level-1 行情 3s 推送间隔，
      不会在流动性枯竭时加剧抢单；tick 内单计划异常隔离（见 execution.py）。
    - 接口与状态机边界：trading_service.get_status 非 live 时跳过本轮，不查行情/
      不下单（断线瞬间行情/下单均不可靠，避免误判离场/重复发废单）。
    - 时区一致性：beat 时区固定 Asia/Shanghai，crontab 按东八区触发（A 股日历对齐）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from celery import Celery
from celery.schedules import crontab

from caisen.config import StrategyConfig
from caisen.execution import ExecutionEngine
from config import CELERY_CONFIG
from server.schemas.caisen import ScanRequest
from server.services import caisen_service, trading_service


# 模块级 logger：beat 任务调度异常需落日志（实盘可观测性——"为什么今天没扫描/
# 没监控"需可追溯，不静默吞）。
logger = logging.getLogger(__name__)


# Why Celery(..., broker/backend)：单 Redis 同时承担消息中间件与结果后端，
# 极简拓扑、运维单点；实例化不建连接（lazy），保证无 Redis 也可 import。
celery_app = Celery("quanter",
                    broker=CELERY_CONFIG["broker_url"],
                    backend=CELERY_CONFIG["broker_url"])
celery_app.conf.task_default_queue = CELERY_CONFIG["queue"]


# ============================================================================
# beat schedule：三任务调度配置
# ============================================================================
# 物理意图（蔡森形态学流水线全自动调度）：
#   - caisen-scan-daily       crontab(hour=15, minute=30)
#       A 股 15:00 收盘，15:30 留 30min 缓冲等收盘数据落盘后再跑全市场扫描，
#       避免扫描时当日 K 线尚未完成导致形态误判。
#   - caisen-monitor-pullback 60.0s
#       盘中每 60s 驱动 ARMED→FILLED 状态机（触及回踩区间即限价买入）。
#       任务内判交易时段——非交易时段 beat 空转直接 return，避免无意义计算。
#   - caisen-monitor-holding  60.0s
#       盘中每 60s 驱动 FILLED→CLOSED 状态机（止损/止盈/时间止损命中即平仓）。
#
# 时区固定 Asia/Shanghai：crontab 按东八区触发，与 A 股交易日历严格对齐。
celery_app.conf.beat_schedule = {
    "caisen-scan-daily": {
        "task": "caisen.scan_universe",
        "schedule": crontab(hour=15, minute=30),
    },
    "caisen-monitor-pullback": {
        "task": "caisen.monitor_pullback",
        "schedule": 60.0,
    },
    "caisen-monitor-holding": {
        "task": "caisen.monitor_holding",
        "schedule": 60.0,
    },
}
celery_app.conf.timezone = "Asia/Shanghai"


# ============================================================================
# 内部工厂：ExecutionEngine 装配（便于测试 monkeypatch 替换）
# ============================================================================
def _build_execution_engine() -> ExecutionEngine:
    """装配 ExecutionEngine（注入 trading_service 单例 + 默认 StrategyConfig）。

    物理意图：盘中监控任务需要 ExecutionEngine 编排 tick_pullback/tick_exit，
    引擎依赖 trading_service（过 10 关风控 + EMT 网关）与 StrategyConfig
    （check_exit 离场参数 + check_pullback 回踩区间）。

    Why 独立工厂函数：便于测试 monkeypatch 替换为 MagicMock（隔离 I/O + 状态机），
    生产装配逻辑集中可维护（trading_service 单例 + 默认 cfg，未来扩展点统一在此）。

    follow-up：trading_service 此处取模块级 import 引用（get_gateway lazy 单例），
    未连接/锁定时 ExecutionEngine.tick_* 内部会自行判 get_status() 跳过——本工厂
    不重复校验连接态（单一真理源：连接态判断只在 tick 编排入口做一次）。
    """
    return ExecutionEngine(trading_service=trading_service, cfg=StrategyConfig())


# ============================================================================
# 任务一：T 日收盘扫描（全市场 universe + 当日 date）
# ============================================================================
@celery_app.task(name="caisen.scan_universe")
def scan_universe() -> list:
    """T 日收盘扫描 beat：调 caisen_service.run_scan 跑全市场扫描→生成→落盘。

    物理意图（编排链路）：
        委托 caisen_service.run_scan 完成全链路：
        screener.screen → plan.generate → storage.save_plans → CandidatePlan。
        本任务仅负责"触发 + 装配 ScanRequest 入参"，扫描算法在 caisen_service 内。

    入参装配：
        date:     当日（YYYY-MM-DD），用作 plans/<date>.json 文件名 + macro 定位；
        universe: 全市场标的池。
                  follow-up：当前 Phase 3 data_lake 未接，caisen_service._load_price_data
                  收到空 universe 返回空 dict → run_scan 按契约降级返回空列表（不抛错）。
                  Phase 3+ 接 data_lake 后应在此装配真实全市场 symbol 列表（如
                  沪深 300 / 中证 500 / 全 A 成分），扫描才能产出真实候选计划。
        cfg_override: 默认空 dict（用默认 StrategyConfig，未来可按需从 env/配置读）。

    防御性：run_scan 内部对算法/IO 异常 try/except 降级返回空列表 + warning 日志
    （见 caisen_service.py），本任务不重复捕获——透传空列表即可，不抛异常到 beat
    调度器（避免 beat 单次失败触发连锁告警）。

    注：amount 单位待 Phase 3 统一（data_lake 千元 vs risk 元）——流动性过滤在
    screener 内部执行，本任务不重复校验（caisen_service.run_scan 已标注 follow-up）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    # 全市场 universe 占位：Phase 3+ 接 data_lake 后装配真实全市场 symbol 列表。
    # 当前传空列表，run_scan 收到空 universe 按契约直接返回 []（不进入扫描链路）。
    # 这是已知降级（非 Bug）——契约层入口先就位，待 data_lake 接入后生产扫描生效。
    req = ScanRequest(date=today, universe=[], cfg_override={})
    try:
        plans = caisen_service.run_scan(req)
        logger.info("scan_universe 完成（date=%s）：生成 %d 个候选计划", today, len(plans))
        return plans
    except Exception as exc:
        # 兜底：run_scan 已对算法/IO 异常降级，此处捕获的应是参数/状态机异常
        # （ValidationError/ValueError/KeyError）——beat 不应因扫描异常崩，落 error
        # 日志后返回空列表，等下一日 beat 重试（蔡森流水线为日级调度，单日失败可容忍）。
        logger.error(
            "scan_universe 异常（date=%s）：type=%s detail=%s",
            today, type(exc).__name__, exc, exc_info=True,
        )
        return []


# ============================================================================
# 任务二：盘中回踩监控（ARMED→FILLED）
# ============================================================================
@celery_app.task(name="caisen.monitor_pullback")
def monitor_pullback() -> None:
    """盘中回踩监控 beat：交易时段 + live → ExecutionEngine.tick_pullback。

    双闸门跳过（断线保护 + 交易时段挡板）：
        1. trading_service._in_a_share_session() == False → return
           非交易时段（隔夜/周末/午休）行情不更新、挂单无意义，beat 空转直接 return；
        2. trading_service.get_status()["mode"] != "live" → return
           网关 unavailable/disconnected/vetoed_by_risk 时断线不补发——行情/下单均
           不可靠，本轮跳过，等下一轮重连后再处理（避免误判回踩触发 / 重复发废单）。

    async 包裹：tick_pullback 是 async 方法（含 await submit_order），Celery 同步
    任务内用 asyncio.run() 驱动事件循环（每次创建新 loop，prefork worker 安全）。
    """
    # —— 闸门 1：非交易时段直接 return（隔夜/周末空转保护）——
    if not trading_service._in_a_share_session():
        return
    # —— 闸门 2：网关非 live 直接 return（断线不补发）——
    # 仅 live 才进入编排：unavailable/disconnected/vetoed_by_risk 均跳过。
    status = trading_service.get_status()
    if status.get("mode") != "live":
        logger.debug(
            "monitor_pullback 跳过（网关非 live，mode=%s）", status.get("mode")
        )
        return

    # —— 进入编排：调 ExecutionEngine.tick_pullback（asyncio.run 包裹 async 方法）——
    # asyncio.run 边界：当前 prefork worker（默认同步执行模型）每次 beat 新建一个
    # 临时 event loop 驱动 async tick——安全。若后续切 gevent/eventlet 池，或
    # ExecutionEngine 引入持久 HTTP session（跨 tick 复用连接池/aiohttp.ClientSession），
    # asyncio.run 每次新建 loop 会导致 session 复用断裂（session 绑定到已关闭的旧 loop
    # 上抛 RuntimeError）——届时需改为持久 loop（worker 级单例 loop / asynccelery）。
    engine = _build_execution_engine()
    try:
        asyncio.run(engine.tick_pullback())
    except Exception as exc:
        # tick_pullback 内部已对单计划异常 try/except 隔离（见 execution.py），
        # 此处捕获的应是 engine 装配/storage 读取层面的异常——beat 不应崩，
        # 落 error 日志后等下一轮 beat 重试（60s 周期，单轮失败可容忍）。
        logger.error(
            "monitor_pullback 异常：type=%s detail=%s",
            type(exc).__name__, exc, exc_info=True,
        )


# ============================================================================
# 任务三：盘中持仓离场监控（FILLED→CLOSED）
# ============================================================================
@celery_app.task(name="caisen.monitor_holding")
def monitor_holding() -> None:
    """盘中持仓离场监控 beat：交易时段 + live → ExecutionEngine.tick_exit。

    双闸门跳过（同 monitor_pullback 语义）：非交易时段 / 网关非 live 直接 return。

    物理意图：盘中每 60s 遍历 FILLED 持仓，check_exit 命中止损/止盈/时间止损
    即市价平仓（FILLED→CLOSED），并推进移动止盈止损上移（update_plan）。
    """
    # —— 闸门 1：非交易时段直接 return ——
    if not trading_service._in_a_share_session():
        return
    # —— 闸门 2：网关非 live 直接 return（断线不补发）——
    # ⚠️ 离场停摆风险（vetoed_by_risk）：trading_service 触发风控锁时 mode 会变为
    # "vetoed_by_risk"，本闸门将其与 disconnected 同等跳过——已有 FILLED 持仓的
    # 止损/止盈/时间止损将暂停。当前采取保守策略（风控锁时不发单，避免与风控决策
    # 冲突）。follow-up：是否在 vetoed_by_risk 时仅停 pullback（新开仓）但保留 exit
    # （离场）监控，让止损/止盈继续生效，需风控策略决策——离场与开仓风控语义不同，
    # 持仓风控原则上必须持续运行，待评审后决定是否拆分跳过逻辑。
    status = trading_service.get_status()
    if status.get("mode") != "live":
        logger.debug(
            "monitor_holding 跳过（网关非 live，mode=%s）", status.get("mode")
        )
        return

    # —— 进入编排：调 ExecutionEngine.tick_exit（asyncio.run 包裹）——
    # asyncio.run 边界：同 monitor_pullback——prefork worker 安全；若切 gevent/eventlet
    # 池或 ExecutionEngine 引入持久 HTTP session（跨 tick 复用），asyncio.run 每次新建
    # loop 会致 session 复用断裂，需改持久 loop（worker 级单例 loop / asynccelery）。
    engine = _build_execution_engine()
    try:
        asyncio.run(engine.tick_exit())
    except Exception as exc:
        # tick_exit 内部已对单持仓异常 try/except 隔离，此处捕获 engine 装配层异常。
        # 离场监控异常不可容忍 tick 崩溃——落 error 日志后等下一轮 beat 重试，
        # 避免单轮异常导致整个离场监控停摆（持仓风控必须持续运行）。
        logger.error(
            "monitor_holding 异常：type=%s detail=%s",
            type(exc).__name__, exc, exc_info=True,
        )
