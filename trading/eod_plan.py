# -*- coding: utf-8 -*-
"""trading.eod_plan — T 日盘后计划生成（集群 D · 颈线法信号 → 计划参数 + trade_event）。

物理定位（T1 模块化拆分 Task 5 · 集群 D）：
    本模块承载盘后计划生成域的两个符号：
    - ``compute``（原 engine.eod_plan 模块级核心 ~157 行）：T 日盘后扫信号 → PlannedOrder
      → 嵌套 order_dict 序列化 → 落 DB trade_event(SIGNAL/CONFIRMED) + 推钉钉（不真下单）。
    - ``sanity_check_date_alignment``（原 TradingEngine._sanity_check_date_alignment 类方法，
      不读 self → free function）：M3 启动口径自检（next_trading_day(today) != today）。

    ``_eod`` / ``_pipeline_then_eod`` cron wrapper **留 engine**（深剖 §2.D：wrapper 形态
    是解耦伏笔，留 engine 调 orchestrate），经 engine re-export ``eod_plan = compute`` 反查
    本模块（``await eod_plan(...)`` 在 _eod wrapper 内经模块全局名解析命中 re-export 别名）。

迁出纪律（strangler 红线①）：函数逻辑【零改动】，只搬位置（trading/engine.py → trading/eod_plan.py）。
    trade_event SIGNAL/CONFIRMED 落 DB + plan 参数序列化逐行原样（幂等红线零容忍 ·
    fill 表 UNIQUE + SIGNAL UNIQUE 守卫不变）。

命名（brief Step 2 · 避免模块名=函数名歧义）：
    原 ``eod_plan`` 函数迁出后命名 ``compute``（``eod_plan.compute`` 清晰）；engine 经
    ``from trading.eod_plan import compute as eod_plan`` re-export 保旧调用全兼容：
      - ``_eod`` wrapper 内 ``await eod_plan(...)``（engine 模块全局名解析 → re-export 别名）；
      - 外部 ``from trading.engine import eod_plan``（scripts/archive/smoke_trading_engine.py
        等历史入口）；
      - 测试 ``monkeypatch.setattr(engine, "eod_plan", _fake)`` / ``engine.eod_plan(...)`` 直调
        （test_engine.py / test_e2e_trading_flow.py / test_c8_date_param 等，经 engine 模块
        属性访问 → re-export 别名 = compute 真身）。

依赖与 patch 路径判断（brief 关键上下文 §3）：
    ``compute`` **不调任何 ``_load_*``**（signals/atr_map/capital 均由 _eod wrapper 传入参数）
    → 无 Task 4 那种 ``patch("trading.engine._load_*")`` 命中风险。模块级符号访问口径：
    - ``_trade_cfg`` / ``_mode``：顶部 ``from trading.critical import`` 直接拿（critical 是 SSoT，
      纯 env 读函数；eod_plan 路径无测试 ``patch("trading.engine._mode"/"_trade_cfg")`` 命中
      compute 内部的需求——_mode 仅用于 logger.info 尾行日志，_trade_cfg 仅用于读 env 参数）。
    - ``_resolve_account_id``：本模块内复制同口径 2 行实现（与 trading.tools.veto_plan /
      trading.gateway_service 同范式——避免循环 import engine，engine
      顶部 re-export eod_plan 时反向 import 会循环）。三处复制以注释锁同步，口径必须一致
      （否则 eod_plan 写 account_A / pre_open 读 account_B，trade_id 对不上 → SIGNAL/VETOED
      防线失效，致命）。
    - ``state_store`` / ``trading_plan`` / ``build_orders_from_signals``：顶部直接 import
      （项目级模块；测试 ``monkeypatch.setattr(trading_plan, "push_plan_to_dingtalk", ...)`` /
      ``monkeypatch.setattr(state_store, "_DEFAULT_DB", ...)`` 走模块对象属性，engine/eod_plan
      共享同一模块对象 → patch 即在模块对象上生效，compute 内调用命中 mock）。
    - ``calendar`` / ``clock``：顶部 ``from trading import``（sanity_check_date_alignment 用；
      ``monkeypatch.setattr("trading.engine.calendar.next_trading_day", ...)`` 在共享模块对象
      上设属性，本模块 ``calendar.next_trading_day`` 同一对象 → 命中）。

logger 名硬编码 ``trading.engine``（与 order_state.py 同口径）：原 eod_plan 经 engine.py
    ``logger = logging.getLogger(__name__)``（__name__=trading.engine）打到 trading.engine
    logger。迁 eod_plan.py 后保 logger 名不变 = 观测面等价（运维按 trading.engine 过滤/聚合
    盘后日志不断 + caplog 断言命中）。
"""
from __future__ import annotations

import json
import logging
import os

from trading import calendar, clock, state_store as _state_store, trading_plan
# W1-B（Task 10）：gateway lazy 顶部化·模块对象风格——调用点经 ``gateway_service.<attr>``
# 属性访问（调用时读模块属性），patch("trading.gateway_service.get_positions") 命中语义
# 与原函数内 lazy import 完全等价；from-import 本地绑定反而会冻结 patch（禁用）。
from trading import gateway_service
# build_orders_from_signals：颈线法信号 → PlannedOrder 纯函数（functional core，trading.compute.plan）。
from trading.compute.plan import build_orders_from_signals
# _trade_cfg / _mode：交易参数 + 模式纯 env 读函数（critical 是 SSoT，T1-Task2 已从 engine 迁出）。
# 顶部直接 import critical（非 engine）——critical 是独立基础设施域，不反向 import 本文件，无循环；
# 且 eod_plan 路径无测试 patch engine._mode/_trade_cfg 命中 compute 内部的需求。
from trading.critical import _mode, _trade_cfg

# logger 名硬编码 trading.engine（而非 __name__=trading.eod_plan）：eod_plan 原是 engine 模块级
# 函数，日志/异常打到 trading.engine logger。迁出后保 logger 名不变 = 观测面等价（运维按
# trading.engine 过滤盘后计划日志不断 + caplog 断言命中）。与 critical.py（用 trading.critical）
# 不同：eod_plan 的盘后计划生成本质是 engine 运行时业务日志。
logger = logging.getLogger("trading.engine")


# H3/T2 收口（2026-08-12）：_resolve_account_id 单一真相源在 trading/account.py。
# 不再本地复制（原复制为避免循环 import engine；account 模块无环，可直 import）。
from trading.account import resolve_account_id as _resolve_account_id


# ============================================================================
# 触发点 1：compute —— T 日盘后扫信号、落计划、推钉钉（不真下单）
# ============================================================================
async def compute(date: str, signals: list, atr_map: dict, capital: float) -> dict:
    """T 日盘后：颈线法信号 → 计划落盘（confirmed=False） → 推钉钉等研究员确认。

    物理意图（术语对齐物理时序 · Task 7b fix）：
        本函数由 ``_eod`` 在 **T 日盘后 19:00** 调用（Task6 时序修复：原 15:35 因增量
        采集 @18:00 尚未落湖致读到 T-1 数据，挪 19:00 等数据落湖 + 检查点② 通过），扫 T 日新突破信号，产 T+1 日
        生效计划；机器批量扫信号易受数据瑕疵/前视偏差/极端行情误判，T 日盘后必须给人
        一次否决机会——故本函数只产计划不下单（spec §2 确认闸红线）。

    Args:
        date:     T+1 日（计划生效日），如 "2026-07-22"。由 _eod 传
                  ``calendar.next_trading_day(today)``（T 日盘后算次日交易日），物理上
                  计划在 T+1 日 pre_open 挂单执行（date 与 load_plan 读取口径对齐）。
        signals:  NecklineMethodStrategy.scan_live 返回的 list[Signal]（Layer2 阶段1 后为
                  frozen dataclass，_eod 已用 dataclasses.replace 注入实验归因字段）。
        atr_map:  {symbol: ATR}，缺 ATR 的标的（_eod 已过滤）在 build 阶段被跳过（不抛）。
        capital:  总资金（仓位 cap 计算基准）。

    Returns:
        {"date":..., "n_orders":..., "mode":...}，n_orders=0 亦正常（当日无信号）。

    嵌套 orders 结构（scope #1，与 Task8 push_plan_to_dingtalk 全链路一致）：
        [{"order":{symbol,qty,side,price}, "stop_price":..., "take_profit":...}, ...]
    """
    cfg = _trade_cfg()
    # 信号 → PlannedOrder（仓位整手 + 止损/止盈价）；缺数据跳过不抛
    orders = build_orders_from_signals(
        signals,
        capital=capital,
        pos_cap=cfg["pos_cap"],
        atr_map=atr_map,
        stop_cfg={
            "stop_atr_mult": cfg["stop_atr_mult"], "tp_h_mult": cfg["tp_h_mult"],
            "max_wait": cfg["max_wait"],
            # Task 7：tp1_h_mult/tp1_portion 透传 build_orders 算 tp1 锁利位
            "tp1_h_mult": cfg["tp1_h_mult"], "tp1_portion": cfg["tp1_portion"],
            # Task 9（D11）：cancel_thresh_mult 透传 build_orders 算 pending 期撤单阈值
            # （颈线+cancel_thresh_mult×H，对齐 simulate_exit:128-129）。env 缺省 0.75。
            "cancel_thresh_mult": cfg.get("cancel_thresh_mult"),
        },
    )
    # 序列化为嵌套 dict（Task8 契约硬约束：order + stop_price + take_profit 三段）
    # 透传 experiment_id/experiment_weight 归因（Task5 PlannedOrder 携带）：
    # Why：report 阶段需按 experiment_id 聚合实验分组，归因字段必须随 order_dict
    # 一起落盘到 trading_plan JSON，否则 Task8 拿不到实验归因的物理基础。
    # 老计划（无归因字段）由 load_plan / report 阶段向后兼容归「未归因」桶，不在此处处理。
    order_dicts = [
        {
            "order": {
                "symbol": o.order.symbol,
                "qty": o.order.qty,
                "side": o.order.side,
                # 挂单价 round 两位（A 股报价粒度=分；颈线回踩挂单同此约束）
                "price": round(o.order.price, 2),
            },
            # 止损/止盈/颈线 round 两位四舍五入：A 股最小报价 0.01 元，算法产出的高精度
            # 浮点（如 stop=10.350317475825085）无实际意义，且污染人审/复盘可读性。
            # Why 序列化层 round 而非 compute 层：compute 是 functional core 保持全精度
            # （风控参数计算精度不折损），仅落盘+展示约束两位（IO 层关切）。
            "stop_price": round(o.stop_price, 2),
            "take_profit": round(o.take_profit, 2),
            # 颈线价（形态基准 c*）：透出供 push_plan_to_dingtalk md 显式标注「颈线」，
            # 让研究员 T-1 晚人审时一眼确认形态基准位（止损/止盈均以颈线为锚）。
            # scan_live 下 entry_price=neckline，故与 order.price 同值——显式单列是为
            # 语义清晰（挂单价=回踩位 vs 颈线=形态位），未来 entry≠neckline 时自然分叉。
            "neckline": round(o.neckline, 2),
            # 地基字段（live-readiness Task 2）：atr 供 trailing 盘后演进 / formed_at 供 pre_open
            # max_wait 窗口判定 / max_wait 有效期。精度：atr round 4 位（计算用），其余原值。
            "atr": round(o.atr, 4),
            "formed_at": o.formed_at,
            "max_wait": o.max_wait,
            # 分级止盈（Task 7 · P0-3）：tp1 锁利位 + tp1_portion 分配比例。
            # Why 落盘：_place_take_profit 盘中买单成交时读 plan.tp1/tp1_portion 拆两张卖单，
            # 必须在 plan JSON 持久化（盘中进程重启 / 切换执行机都能读回）。
            # tp1 可能为 None（老 cfg 不配 tp1_h_mult）→ JSON null，_place_take_profit 检测
            # falsy 退回 tp2 单笔全平（向后兼容）。
            "tp1": round(o.tp1, 2) if o.tp1 is not None else None,
            "tp1_portion": o.tp1_portion,
            # Task 9（D11）：cancel_on pending 期撤单阈值落盘（颈线+cancel_thresh_mult×H）。
            # Why 落盘：_stoploss pending_ctx 盘中读 plan.cancel_on 监控 high≥此价撤买单
            # （对齐 simulate_exit:130 skip_target_met）。None（cfg 未配 cancel_thresh_mult）
            # → JSON null，_stoploss 检测 falsy 不塞 pending_ctx（不撤单放飞，向后兼容）。
            "cancel_on": round(o.cancel_on, 2) if o.cancel_on is not None else None,
            "experiment_id": o.experiment_id,           # 透传实验归因（Task5 → Task8 链路）
            "experiment_weight": o.experiment_weight,   # 透传实验权重（Task8 加权聚合用）
            # R3 实际口径盈亏比（2026-07-27 Task 2）：从 PlannedOrder.rr 透传到 order_dict，
            # 供 push_plan_to_dingtalk md 渲染「盈亏比N.N」+ trading_plan JSON 落盘存档。
            # Why 落盘：研究员复盘时可按 rr 排序找出弱信号历史，迭代 min_rr 守卫阈值。
            "rr": round(o.rr, 2),
        }
        for o in orders
    ]
    # SSoT Phase C · C3：写盘路径已删（DB SIGNAL/CONFIRMED 是真相源）。
    # 确认闸（AUTO_CONFIRM_PLAN=true 全自动 → 写 DB CONFIRMED；默认 false 人审）：
    # pre_open 次日据 DB latest_action=CONFIRMED 直挂（opt-out：研究员 pre_open 前 veto 拦截）；
    # 默认 false → DB 只写 SIGNAL 无 CONFIRMED → pre_open 据未确认跳过（spec §2 红线）。
    auto_confirmed = os.getenv("AUTO_CONFIRM_PLAN", "").lower() in ("true", "1", "yes")
    # state-store-redesign §3.3 · C3 真相源：DB trade_event(SIGNAL, meta=计划参数) 唯一真相源。
    # SIGNAL 幂等（UNIQUE account_id+trade_id+action）：重跑 eod_plan 已存在则跳过（不重复记）。
    # account_id 缺省走默认账户（_migrate_env_to_account 在启动期落真实账户）。
    # C2c：pre_open/_stoploss/review_report/review_service 全切 DB SIGNAL.meta；C3：load_plan
    # DB 优先 + JSON 只读兼容窗口（C3 删写路径后 JSON 不再被生产代码写盘）。
    try:
        account_id = _resolve_account_id()
        # 确保 account 行存在（trade_event/order FK 引用；init_store 只建表不插行）
        if _state_store.get_account(account_id) is None:
            _state_store.upsert_account(account_id, broker="qmt")
        for o in order_dicts:
            sym = (o.get("order") or {}).get("symbol")
            if not sym:
                continue
            trade_id = _state_store.build_trade_id(account_id, sym, date)
            # SIGNAL meta 存计划参数快照（stop_loss/pre_open 改从 DB 读，spec §3.3）
            # SSoT Phase C · C1：meta 补 plan_date/strategy_name/rationale（C2 前置 + 归因重建真相源）。
            # 物理意图（C2 前置语义）：
            #   - plan_date=date（T+1 计划生效日，与 trade_id 同口径）：C2a scan_count 按 plan_date
            #     LIKE 查；C2d experiment report/trigger 按 plan_date 聚合。timestamp=写入日 T ≠ 计划
            #     日 T+1（致命日期轴，若按 timestamp 查会把 T 日盘后写入归到 T 日计划，错位一天）。
            #   - strategy_name="neckline"（当前单策略，未来多策略时由 build_orders 透传）：C2c
            #     review_report/review_service 按 strategy_name 过滤；rebuild_position_attribution 读
            #     真实 strategy_name 回填 position（弥补 B2 重启窗口）。
            #   - rationale=「颈线法@{formed_at}」（人类可读归因，formed_at=T 信号突破日）。
            # 非破坏扩展：{**o, ...} 在原 order_dict（stop_price/take_profit/neckline/formed_at/
            #   experiment_id 等）基础上扩展，不动 o 既有字段——消费方读 meta 不受影响。
            meta_obj = {**o, "plan_date": date, "strategy_name": "neckline",
                        "rationale": f"颈线法@{o.get('formed_at', '')}"}
            _state_store.insert_trade_event(
                account_id, trade_id, sym, "SIGNAL",
                meta=json.dumps(meta_obj, ensure_ascii=False))
            # CONFIRMED 仅在 auto_confirmed 且未被 veto 时写（veto 保护：最新 action=VETOED 不覆盖；
            # M2 收口：is_vetoed 单点封装 None 安全的 ==VETOED 判断，字面量不再散落）
            if auto_confirmed and not _state_store.is_vetoed(trade_id):
                _state_store.insert_trade_event(account_id, trade_id, sym, "CONFIRMED")
    except Exception:
        # DB 写失败不阻断 eod_plan 主流程（C3：DB 是真相源，但失败软降级不抛，下次 eod 补写）
        logger.exception("eod_plan 落 trade_event(SIGNAL) 失败（不阻断主流程，软降级）")
    # C3：删 confirm 调用——auto 路径的 DB CONFIRMED 已在上方 642-644（W2）写。
    # 原确认函数是 JSON 写盘+DB CONFIRMED 双写对齐，C3 删写路径后 DB CONFIRMED
    # 由 eod_plan auto 路径单点写（顺序：SIGNAL 先、CONFIRMED 后，保证 latest=CONFIRMED）。
    # 持仓段注入 QMT 真实持仓（全量口径，和持仓播报同源）：研究员钉钉人审要看券商实际
    # 仓位（含 T+1 冻结），而非 engine 记账（dry_run 空仓 + 不含 smoke 直接 broker 操作）。
    # 网关未连/异常 → None，push 内部退回 position_book 本地账本（软降级，不阻断推送）。
    broker_positions = None
    try:
        # W1-B：顶部模块对象访问（原函数内 lazy import 已顶部化，patch 语义等价）。
        broker_positions = await gateway_service.get_positions()
    except Exception:
        logger.warning("eod_plan 拉 QMT 持仓失败，交易计划持仓段退回本地账本")
    trading_plan.push_plan_to_dingtalk(
        date, order_dicts, broker_positions=broker_positions, auto_confirmed=auto_confirmed)
    logger.info("eod_plan 完成 date=%s n_orders=%d mode=%s auto_confirmed=%s",
                date, len(orders), _mode(), auto_confirmed)
    return {"date": date, "n_orders": len(orders), "mode": _mode(), "auto_confirmed": auto_confirmed}


# ============================================================================
# M3 口径自检：eod 落盘 key(next_trading_day) 与 pre_open 读 key(today) 对齐
# ============================================================================
def sanity_check_date_alignment(today: str | None = None) -> bool:
    """M3：启动口径自检——确认 _eod 落盘用 next_trading_day、_pre_open 读 today。

    Why（[[eod-date-offbyone-fix]] 主动防线）：
        代码口径已修（_eod 传 next_trading_day(today) 落盘、_pre_open 次日读 today），
        但若进程跑的是未重启的旧代码（口径退回 today 落盘 → 次日读 T+1 永远差一天），
        会直接导致「标的错位 + 永不挂单」——这是已知的静默致命 bug，进程级口径不可见。
        本自检在 start() 注册 cron 前主动验证 ``calendar.next_trading_day(today) != today``
        （即确实算出了次日而非原样返回），否则视为口径坏，让调用方降级（dry_run + 告警）。

    Args:
        today: YYYY-MM-DD；None 时取 clock.today() 当日（启动自检默认当日）。

    Returns:
        True  = 口径正常（next_trading_day 算出次日，落盘 key 与次日读 today 对齐）；
        False = 口径异常（next_trading_day 返 today 自身/空值/抛异常 → 疑似跑旧代码，
                调用方 start() 须 logger.error 告警，CRITICAL 钉钉接线留 T9）。

    T1-Task5 迁出说明：原 ``TradingEngine._sanity_check_date_alignment(self, today)`` 类方法
        不读 self（仅 clock.today + calendar.next_trading_day 纯判定）→ 逻辑迁本 free function。
        engine 类留薄实例 wrapper ``_sanity_check_date_alignment(self, today)`` 调之，保
        ``eng._sanity_check_date_alignment()``（test_engine_sanity_check /
        test_e2e_trading_flow 测试）+ ``self._sanity_check_date_alignment()``（start() 调用）命中。
    """
    # C-6 V2：启动口径自检的当日基准走 clock.today（单一时间源口子）。
    _today = today or clock.today()
    try:
        nxt = calendar.next_trading_day(_today)
    except Exception as exc:
        # next_trading_day 抛异常（日历源故障/网络异常）同样判口径坏（保守拒进 live）
        logger.exception("【口径自检失败】next_trading_day(%s) 抛异常，判口径坏：%s", _today, exc)
        return False
    if not nxt or nxt == _today:
        # 旧 bug 口径：next_trading_day 原样返回 today（或空值）→ 落盘 key=今日，
        # 次日 pre_open 读 today 永远差一天 → 标的错位 + 永不挂单（静默致命）。
        logger.error(
            "【口径自检失败】next_trading_day(%s)=%s 未算出次日（疑似跑旧代码口径），"
            "拒绝进 live（降级 dry_run，CRITICAL 钉钉告警待 T9 接入）", _today, nxt)
        return False
    logger.info("口径自检通过：eod 落盘 key=%s，pre_open 次日读 today 与之对齐", nxt)
    return True
