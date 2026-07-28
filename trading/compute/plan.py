# -*- coding: utf-8 -*-
"""trading.compute.plan — 颈线法信号 → 下单计划转换纯函数（functional core）。

物理定位（Layer2 阶段2 · spec §3.5/§4）：
    build_orders_from_signals 是【纯函数】——输入信号列表 + 资金/参数，输出
    PlannedOrder 列表，无 I/O、无状态、确定性。回测与实盘共用同一计划生成逻辑
    （杀手不变量）。本模块仅依赖标准库 + trading.compute.types + strategies.signal
    （Signal 是 frozen dataclass 纯数据契约），零外部 I/O 依赖。

把 NecklineMethodStrategy.scan_live 返回的 Signal dataclass 转成 PlannedOrder
（OrderRequest + 止损/止盈价）。仓位：capital × pos_cap × experiment_weight /
entry_price，向下取整到 100 整手（A 股）。止损/止盈：颈线基准 + ATR/H（与
simulate_exit 同口径）。

Layer2 阶段1：信号入参从 ``list[dict]`` 收敛为 ``list[Signal]``（frozen dataclass），
本函数改读 ``signal.symbol / signal.entry_price / ...`` 属性，去字符串键访问。

实验归因（Task 5）：
- PlannedOrder 新增 experiment_id / experiment_weight 两个默认值字段，把实验系统的
  “这个信号属于哪个实验版本、应当分配多少资金权重”的归因信息从 _eod 透传到下单层，
  便于 Task 8 report 按实验聚合 PnL，以及灰度阶段同时跑多版本但各自只占一部分仓位。

迁移纪律（strangler 红线①）：函数逻辑【零改动】，只搬位置（trading/signal_runner.py
→ trading/compute/plan.py）。原 trading/signal_runner.py 垫片已在 Layer2 阶段6
follow-up #4a 删除——所有消费点（engine.py + 3 tests 含 e2e）已改指真身
``from trading.compute.plan import build_orders_from_signals, PlannedOrder``。
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.compute.types import OrderRequest
from strategies.signal import Signal


@dataclass
class PlannedOrder:
    """计划单（OrderRequest + 出场价 + 实验归因 + 实际盈亏比）。

    新增 experiment_id/experiment_weight 两个默认值字段：
    - 默认值保证老调用点（不带归因）零回归；
    - 实验系统启用后，Task 7 的 _eod 会在 signal dict 里注入，此处原样落盘。
    新增 rr 字段（R3 · 2026-07-27 Task 2）：
    - 实际口径盈亏比 = (tp2-entry)/(entry-stop_price)，从 Signal.rr 透传，
      用于 push_plan_to_dingtalk md 展示「盈亏比N.N」让研究员人审看真实风险报酬比。
    - 默认 0.0 保证老 Signal（无 rr）零回归，push 层判 falsy 跳过渲染。

    新增 tp1/tp1_portion（plan Task 7 · 对齐缺口 P0-3）：
    - tp1 = 颈线 + tp1_h_mult × H：颈线法分级止盈的第一档锁利位（轻仓提前兑现，
      避免回踩破位把全部浮盈回吐）。_place_take_profit 据此挂 tp1 限价卖单。
    - tp1_portion：tp1 档分配的持仓比例（0~1，默认 0.5）。0 = 全量 tp2（不锁利），
      1 = 全量 tp1（不博 tp2 形态对称目标位）。
    - tp1 默认 None：stop_cfg 缺 tp1_h_mult 时不计算（_place_take_profit 检测 falsy
      退回 tp2 单笔全平，向后兼容 Task 7 前的老 stop_cfg / 老 plan）。
    """
    order: OrderRequest
    stop_price: float
    take_profit: float
    neckline: float
    atr: float = 0.0                 # 地基：信号日 ATR（trailing 演进用，from Signal.atr）
    formed_at: str = ""              # 地基：信号突破日（pre_open max_wait 窗口判定用，from Signal.formed_at）
    max_wait: int = 5                # 地基：挂单等待回踩有效期日（pre_open 超期过滤用，from stop_cfg）
    experiment_id: str = ""          # 归因：所属实验版本（_eod 注入，Task 8 report 按此聚合）
    experiment_weight: float = 1.0   # 归因：落盘时冻结的资金权重（灰度分流，1.0=满仓口径）
    rr: float = 0.0                  # R3 实际口径盈亏比（从 Signal 透传，push 展示用）
    # 分级止盈第一档（Task 7）：tp1=None 表示 stop_cfg 未配置，退回 tp2 单笔全平（零回归）
    tp1: float | None = None
    tp1_portion: float = 0.0         # 分级止盈 tp1 分配比例（默认 0 → 全量 tp2，向后兼容）


def build_orders_from_signals(
    signals: list[Signal],
    *,
    capital: float,
    pos_cap: float,
    atr_map: dict[str, float],
    stop_cfg: dict,
) -> list[PlannedOrder]:
    """信号列表 → PlannedOrder 列表。缺 ATR/数据异常的跳过（不抛）。

    Layer2 阶段1：signals 改为 ``list[Signal]``（frozen dataclass），本函数读
    ``signal.symbol / signal.entry_price / ...`` 属性。归因字段（experiment_id /
    experiment_weight）由 Signal 默认值保证（""/ 1.0），_eod 用 dataclasses.replace
    注入。

    资金分配（Task 5）：budget = capital × pos_cap × experiment_weight
    - 每个信号按各自 experiment_weight 分流资金额度（灰度权重在此落地为实际手数）；
    - 老信号无 experiment_weight → Signal 默认 1.0，budget 与原口径完全一致（向后兼容）。

    Why 跳过而非抛错：自动交易引擎在盘后批处理多只标的时，单只缺 ATR/数据异常
    不应中断整批；此处静默跳过，由上层日志记录后人工补救。
    """
    stop_mult = stop_cfg.get("stop_atr_mult", 2.0)
    tp_mult = stop_cfg.get("tp_h_mult", 2.0)
    # 分级止盈参数（Task 7 · P0-3 对齐）：tp1_h_mult×H 锁利位 + tp1_portion 分配比例。
    # Why 显式 .get + None 兜底：老 stop_cfg（Task 7 前）不传这两键，None 让下游
    # _place_take_profit 检测 falsy 退回 tp2 单笔全平，零回归。
    tp1_mult = stop_cfg.get("tp1_h_mult")
    tp1_portion = float(stop_cfg.get("tp1_portion", 0.0) or 0.0)
    out: list[PlannedOrder] = []
    for s in signals:
        sym = s.symbol
        entry = s.entry_price
        neckline = s.neckline
        bottom = s.bottom
        # ATR 取值（C2 · final-fix）：优先用 signal 自身 atr，fallback atr_map。
        # Why 优先 signal atr：_eod 内 ``atr_map[sym] = s.atr`` 多实验同标的灰度时
        # 被最后写入的实验覆盖 → 共享 atr_map 已无法按实验区分 ATR。Signal 已
        # 携带各自的 s.atr（Task 7a scan_live 返回），用 signal 自身 atr 才能保证
        # 每个 PlannedOrder.stop_price 用各自实验的 ATR（spec §0「参数以不可变快照锁定」
        # —— 红线：止损价是实盘风险参数，跨实验串味 = 风险归因错配）。
        # 老链路 signal 无 atr 字段 → 退回 atr_map（向后兼容，零回归）。
        sig_atr = s.atr
        atr = sig_atr if sig_atr is not None else (atr_map.get(sym) if sym else None)
        if not sym or entry is None or neckline is None or bottom is None or atr is None:
            continue
        # 实验归因：取每信号各自的权重（灰度分流）；Signal 默认 1.0 = 满仓口径，向后兼容
        weight = s.experiment_weight
        # 仓位：capital × pos_cap × weight / entry，向下取整到 100 整手（A 股 1 手=100 股）
        # Why weight 放进 budget：让小权重实验也按整手规则下沉，避免 0~100 股的零股废单；
        # 同权重跨标的资金额度可直接比较，便于回测/实盘对齐归因口径。
        budget = capital * pos_cap * weight
        qty = int(budget / float(entry) / 100) * 100   # 向下取整 100 整手
        if qty <= 0:
            # 资金不足 1 手 → 放弃，防零股废单（小权重灰度时常见，非异常）
            continue
        # H = 颈线到底部的高度，作为风险报酬比的标尺
        h = float(neckline) - float(bottom)
        # 止损 = 颈线 - stop_mult × ATR（ATR 口径，过滤窄幅噪音）
        stop_price = float(neckline) - stop_mult * float(atr)
        # 止盈 tp2 = 颈线 + tp_mult × H（形态学对称目标位）
        take_profit = float(neckline) + tp_mult * h
        # 分级止盈 tp1 = 颈线 + tp1_h_mult × H（Task 7 · P0-3）：
        # Why tp1<tp2：tp1_h_mult < tp_h_mult（EXEC_DEFAULTS 1.0 vs 2.0）保证 tp1 先于 tp2
        # 成交，在 tp1 锁定部分浮利后剩余博 tp2 形态目标位；simulate_exit 同口径计算。
        # tp1_mult 缺省（None）→ tp1=None，下游 _place_take_profit 退回 tp2 单笔全平（零回归）。
        tp1_price = (float(neckline) + tp1_mult * h) if tp1_mult is not None else None
        out.append(PlannedOrder(
            order=OrderRequest(symbol=sym, qty=float(qty), side="buy", price=float(entry)),
            stop_price=stop_price, take_profit=take_profit, neckline=float(neckline),
            # 地基字段（live-readiness Task 2）：atr 供 trailing 演进 / formed_at 供 pre_open
            # max_wait 窗口判定 / max_wait 从 stop_cfg 读（默认 5 对齐 NecklineConfig）
            atr=float(atr),
            formed_at=str(s.formed_at) if s.formed_at is not None else "",
            max_wait=int(stop_cfg.get("max_wait", 5)),
            # 归因透传：experiment_id Signal 默认 ""（老链路），experiment_weight 已在 budget 落地
            experiment_id=s.experiment_id,
            experiment_weight=weight,
            # R3 实际 rr 透传：Signal.rr 缺省（None）时归 0.0（push 层 falsy 跳渲染，零回归）
            rr=s.rr if s.rr is not None else 0.0,
            # Task 7：tp1/tp1_portion（None/0 → 下游退回 tp2 单笔全平，向后兼容）
            tp1=tp1_price, tp1_portion=tp1_portion,
        ))
    return out
