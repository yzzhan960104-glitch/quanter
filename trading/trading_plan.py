# -*- coding: utf-8 -*-
"""T-1 交易计划（DB 优先真相源 + 钉钉推送 + JSON 只读兼容窗口）。

SSoT Phase C · C3 重构（spec §6）：
    生产写路径已删——原 JSON 落盘/确认函数 移至 ``tests/_legacy_plan_io.py``
    作测试专用 legacy shim（保留原 JSON 落盘 + DB CONFIRMED 双写语义给历史测试种子）。
    生产 eod_plan 直接写 DB trade_event(SIGNAL, meta) + （AUTO_CONFIRM）CONFIRMED 行，
    不再走 JSON 镜像双写。

二期引擎 T-1 确认闸（spec 红线）物理意图（C3 后）：
    eod_plan（T-1 晚 15:35）扫信号生成 orders
    → DB trade_event(SIGNAL, meta=计划参数) 落真相源
    → push_plan_to_dingtalk 推交易机器人群（研究员人审，orders 作入参不落盘）
    → 研究员钉钉回复「确认」（生产路径无）或 AUTO_CONFIRM_PLAN=true 全自动
    → DB trade_event(CONFIRMED) 落放行标志
    → 次日 pre_open（09:22）读 DB latest_action=CONFIRMED 才挂单，未确认/VETOED 不挂。

为什么需要确认闸：机器自动扫信号可能因数据瑕疵/前视偏差/极端行情误判，
T-1 晚给人一次否决机会，防止机器盲发导致不可逆的实盘敞口。

orders 采用嵌套格式（与 Task 9 engine.eod_plan 生产侧、本模块 push_plan_to_dingtalk
消费侧全链路统一）：
    {"order": {symbol/qty/side/price}, "stop_price": ..., "take_profit": ...}
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from broadcast.push import push_brief

logger = logging.getLogger(__name__)


def _plan_path(date: str) -> Path:
    """T 日计划文件路径：<TRADE_PLAN_DIR>/plan_<date>.json。

    TRADE_PLAN_DIR 默认 logs/trading_plans；生产环境由调度器/启动脚本显式注入。

    C3 后本函数仅供 load_plan JSON 回退（只读兼容窗口）使用；无生产写盘方。
    测试种子可用 ``tests/_legacy_plan_io._plan_path`` 同口径读取。
    """
    base = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    return base / f"plan_{date}.json"


def load_plan(date: str) -> dict | None:
    """读计划 · DB 优先（SIGNAL.meta 真相源）+ JSON 回退（C3 只读兼容窗口）。

    物理意图（SSoT Phase C · C3）：单一真相源硬化后，DB trade_event(SIGNAL).meta 是
    「精确 per-symbol 计划参数」真相源（C2c 已切 pre_open/_stoploss/review_report/
    review_service）。本函数消费方契约不变（返 ``{date, confirmed, orders}``）：
        - orders：从 DB SIGNAL.meta 列表构造（每项即原 order_dict + C1 补的
          plan_date/strategy_name/rationale 字段，消费方读 meta.stop_price 等不变）；
        - confirmed：按 per-symbol ``get_latest_action(trade_id)`` 判断——所有标的
          latest=CONFIRMED 才整体确认，任一 VETOED/未确认 → confirmed=False（veto
          终局防线：VETOED 晚于 CONFIRMED → 该标的最新的 action=VETOED → 未确认）。

    **致命日期轴（与 list_signals_with_meta_by_plan_date 同口径）**：
        trade_event.timestamp = T 日盘后写入时间（非计划日 T+1），按 timestamp 查恒空。
        计划日仅在 trade_id 后缀（build_trade_id 单点 ``{account_id}_{symbol}_{date}``），
        故按 ``substr(trade_id,-10)=date`` 查。

    build_trade_id 单点（消原 load_plan 内裸拼 trade_id 的 _account_id 未定义问题）：
        与 eod_plan/_pre_open_impl/veto_plan 完全一致口径，否则 get_latest_action 查的
        trade_id 与写 SIGNAL 的 trade_id 对不上，防线失效。

    回退窗口（C3 只读兼容）：DB 异常 / 无 SIGNAL 行 → 退回读 plan_*.json（如果存在）。
    物理：C2c 切 DB 是渐进的（部分老数据/老测试仍 JSON 落盘），保留一个发布周期的
    只读兼容窗口；C3 删写路径后，JSON 仅历史回退入参，不再
    被生产代码写盘。无 SIGNAL 且无 JSON → None（pre_open 保守跳过挂单，不挂脏计划）。

    Returns:
        ``{date, confirmed, orders}`` dict；无 SIGNAL 且无 JSON 返 None；DB 异常且
        JSON 损坏/不存在也返 None（双重降级，pre_open 据此安全跳过）。
    """
    # —— DB 优先路径 ——
    # 物理意图：DB 是真相源；list_signals_with_meta_by_plan_date 按 trade_id 后缀
    # substr(-10)=date 查（非 timestamp），返每行 SIGNAL 的 meta 解析 dict + symbol。
    try:
        from trading import state_store
        account_id = os.getenv("QMT_ACCOUNT_ID", state_store._DEFAULT_ACCOUNT_ID)
        metas = state_store.list_signals_with_meta_by_plan_date(date)
        if metas:
            # 有 SIGNAL 行 → 走 DB 真相源路径，按 per-symbol latest_action 判 confirmed。
            orders: list = []
            confirmed_all = True
            for m in metas:
                # meta shape：{symbol, **原 order_dict（含 order/stop_price/.../C1 字段）}。
                sym = (m.get("order") or {}).get("symbol", m.get("symbol"))
                # trade_id 单点：build_trade_id（与 eod_plan/veto 同口径，消 _account_id 未定义）
                tid = state_store.build_trade_id(account_id, sym, date)
                action = state_store.get_latest_action(tid)
                # veto 终局防线：VETOED 晚于 CONFIRMED → latest=VETOED → confirmed=False。
                # 其他状态语义（spec §6 / spec §2.4 trade_event 事件流）：
                #   - SIGNAL-only（未确认）→ 未确认（pre_open 据不放行）
                #   - CONFIRMED → 已确认（pre_open 放行）
                #   - ORDERED/FILLED/CLOSED/TP_FILLED → 已确认且已挂单/成交（生命周期晚于
                #     CONFIRMED，这些状态出现意味着 plan 已确认 + 已挂单，视作 confirmed=True
                #     避免 post_close snapshot 等读已成交 plan 时误判未确认）。
                # 故判据：latest == "SIGNAL"（仅 SIGNAL 无 CONFIRMED）= 未确认；
                #         latest == "VETOED" = veto 终局未确认；其余 = 已确认。
                if action in ("SIGNAL", "VETOED", None):
                    # None 理论不会出现（meta 行至少有 SIGNAL），保守视作未确认
                    confirmed_all = False
                orders.append(m)
            return {"date": date, "confirmed": confirmed_all, "orders": orders}
    except Exception:
        # DB 读异常（state_store 未初始化 / 表不存在 / 文件锁等）→ 软降级回退 JSON。
        # 物理：load_plan 是 pre_open/place_take_profit/veto 多入口的依赖，抛错会阻断
        # 调度链；回退 JSON 是「展示镜像」语义（C3 后 JSON 是导出产物，非真相源），
        # 历史数据/老测试仍有 JSON 落盘，保留一发布周期的只读兼容窗口。
        logger.exception("load_plan 读 DB SIGNAL 失败，回退 JSON（C3 只读兼容窗口）")

    # —— JSON 回退（只读兼容窗口）——
    # C3 后无生产写盘方（写路径已删），JSON 仅历史/测试种子用，不再被覆盖写。
    p = _plan_path(date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # 损坏计划：记录完整堆栈供排查，但返 None 让 pre_open 安全降级（不挂单）。
        logger.exception("计划损坏 %s（C3 JSON 回退窗口）", p)
        return None


# ============================================================================
# 注：原 JSON 落盘 / 确认函数 已删除（SSoT Phase C · C3 收尾）。
# 生产 eod_plan 直接写 DB trade_event(SIGNAL/CONFIRMED)（engine.py:619-644）。
# 测试种子若需 JSON 镜像落盘 + DB CONFIRMED 双写语义，import
# ``tests._legacy_plan_io`` 模块的 legacy shim 函数（测试专用）。
# ============================================================================


_NAME_MAP_CACHE = None


def _load_name_map() -> dict:
    """加载 ts_code→中文名称 映射（data_lake/stock_basic.parquet，懒加载缓存）。

    Why 缓存：push_plan_to_dingtalk 每次调不重读 parquet（5531 行 I/O）；首次读后
    模块级复用。读失败返 {}（名称缺失时推送只显代码，不崩——展示降级，不阻断推送）。
    """
    global _NAME_MAP_CACHE
    if _NAME_MAP_CACHE is not None:
        return _NAME_MAP_CACHE
    try:
        import pandas as pd
        sb = pd.read_parquet("data_lake/stock_basic.parquet")
        # 2026-08-05 schema 兼容：stock_basic.parquet 的 ts_code 在 MultiIndex 索引层
        # （index, ts_code），历史版本是 ts_code 列；无 ts_code 时回退 symbol 列。
        if "ts_code" in sb.columns:
            codes = sb["ts_code"]
        elif "ts_code" in sb.index.names:
            codes = sb.index.get_level_values("ts_code")
        else:
            codes = sb["symbol"]
        _NAME_MAP_CACHE = dict(zip(codes, sb["name"]))
    except Exception:
        logger.exception("加载 stock_basic 名称映射失败（推送将只显代码）")
        _NAME_MAP_CACHE = {}
    return _NAME_MAP_CACHE


def _get_positions_snapshot() -> dict:
    """当前本地持仓快照 {symbol: qty}（position_book；dry_run 空仓，live 反映真实成交累计）。

    Why 本地账本而非 broker：push 时研究员看「engine 记账持仓」，与 post_close 对账
    同源；broker 真实持仓在对账环节比对。init_db 幂等兜底（手动调用未走 __main__ 时
    position 表可能未建）。
    """
    try:
        from trading import position_book
        position_book.init_db()
        return position_book.get_local_positions()
    except Exception:
        logger.exception("读本地持仓失败（推送持仓段将显空仓）")
        return {}


def push_plan_to_dingtalk(date: str, orders: list, broker_positions: list | None = None, auto_confirmed: bool = False) -> bool:
    """把 T-1 计划推到交易机器人群（研究员钉钉确认用）。

    复用一期 broadcast.push.push_brief（subprocess 调 dws send-by-bot，
    零自写加签）。格式化嵌套 orders 为 Markdown。

    Args:
        date: 交易日。
        orders: 嵌套格式 list（同 DB SIGNAL.meta order_dict 结构）。

    Returns:
        push_brief 返回值透传：成功 True；缺凭证/超时/dws 不在/returncode≠0 → False（不抛）。
    """
    # 格式化在 try 外也可，但兜底：orders 结构异常时不抛，记堆栈返 False。
    try:
        name_map = _load_name_map()
        # 计划下单段：标的名(symbol) 双显。研究员人工确认闸依赖中文标的名认知，
        # 只显代码认不出 → 必须 name + code（name 缺失时降级只显 code 不崩）。
        # R3（2026-07-27 Task 2）：每单追加「盈亏比N.N」——从 order_dict["rr"] 读，
        # 让研究员 T-1 晚人审快速识别弱信号（rr<1.5 的形态风险报酬比失衡，应人工否决）。
        # 老 order_dict（无 rr 键）→ o.get 返 None → rr_str="" → md 与旧版逐字一致（零回归）。
        lines = []
        for o in orders:
            sym = o['order']['symbol']
            nm = name_map.get(sym, "")
            prefix = f"{nm} " if nm else ""
            rr = o.get("rr")
            # 精度取舍（非 bug）：md 展示用 1 位小数（人审快速识别弱信号足够，避免群里
            # 信息过载），order_dict 落盘用 round(rr, 2)（复盘统计精度保留两位）。
            # 两路精度不同是有意为之——人审足够 vs 复盘精度，分轨处理。
            rr_str = f" 盈亏比{rr:.1f}" if rr else ""
            # 颈线价（形态基准 c*）：显式标出让研究员一眼确认「挂单价=颈线回踩位」，
            # 止损/止盈均以此为锚。老 plan（无 neckline 键）→ .get 返 None → neck_str=""
            # → md 与旧版逐字一致（零回归，与 rr 同款 falsy 跳渲染范式）。
            neckline = o.get("neckline")
            neck_str = f"颈线{neckline:.2f}/" if neckline else ""
            # 价格统一 :.2f（A 股报价粒度=分）：落盘已 round 两位，显示再 :.2f 双保险，
            # 防 10.30 显示成 10.3（保证两位小数对齐，人审可读性）。
            lines.append(
                f"- {prefix}{sym} {o['order']['side']} {o['order']['qty']}股"
                f"@{o['order']['price']:.2f}（{neck_str}止损{o['stop_price']:.2f}/止盈{o['take_profit']:.2f}）{rr_str}"
            )
        # 当前持仓段：优先用调用方注入的 broker_positions（QMT 全量口径，和持仓播报
        # 同源，含成本/现价/盈亏%）；None（网关未连/异常）→ 退回 position_book 本地账本。
        # Why 优先 broker：研究员钉钉人审要看券商真实仓位（含 T+1 冻结仓），position_book
        # 是 engine 记账（dry_run 空仓 + 不含 smoke 等直接 broker 操作），用本地账本会
        # 显示「空仓」误导（实则持有茅台 T+1）。
        if broker_positions is not None:
            pos_lines = []
            for p in broker_positions:
                sym = p.get("symbol", "")
                nm = name_map.get(sym, "")
                prefix = f"{nm} " if nm else ""
                qty = p.get("qty")
                avg = p.get("avg_price"); last = p.get("last_price"); pct = p.get("pnl_pct")
                # 盲价防御：avg/last/pct 任一缺失 → 只显代码+数量（不猜价，与播报同口径）
                if qty is not None and avg is not None and last is not None and pct is not None:
                    pos_lines.append(
                        f"- {prefix}{sym} {qty:.0f}股 成本{avg:.2f} 现价{last:.2f} {pct:+.2f}%"
                    )
                elif qty is not None:
                    pos_lines.append(f"- {prefix}{sym} {qty:.0f}股")
                else:
                    pos_lines.append(f"- {prefix}{sym}")
            if not pos_lines:
                pos_lines = ["- 空仓"]
        else:
            # 软降级：网关未连 → position_book 本地账本（engine 记账）
            local = _get_positions_snapshot()
            pos_lines = [
                f"- {name_map.get(s, '')} {s} {q:g}股" for s, q in local.items()
            ] or ["- 空仓"]
        # 文案随确认模式切（C1 兼容）：auto_confirmed=True=全自动 opt-out（将自动挂单，
        # pre_open 前 veto 可拦）；False=人审 opt-in（回复确认才挂，spec §2 默认）。
        note = ("⏰ 全自动模式：将自动挂单（pre_open 09:22 前 veto_plan.py 可拦）"
                if auto_confirmed else
                "待确认（回复「确认」即挂单）")
        md = (
            f"### T-1 交易计划 {date}\n"
            f"> {note}\n\n"
            f"**计划下单**\n" + "\n".join(lines) + "\n\n"
            f"**当前持仓**\n" + "\n".join(pos_lines)
        )
        # 凭证从环境读：TRADING_BOT_ROBOT_CODE（交易机器人）/ BROADCAST_GROUP_ID（运营群），
        # 与 broadcast __main__.PUSH_BOTS["trading"] / _GROUP_ID_ENV 凭证约定一致。
        robot = os.getenv("TRADING_BOT_ROBOT_CODE", "")
        group = os.getenv("BROADCAST_GROUP_ID", "")
        return push_brief(
            f"交易计划 {date}", md, robot_code=robot, group_id=group
        )
    except Exception:
        # orders 结构漂移/缺 key → 记堆栈返 False，绝不抛到调度器致 cron 整体崩。
        logger.exception("推计划到钉钉失败")
        return False
