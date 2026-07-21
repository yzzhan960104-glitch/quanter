# -*- coding: utf-8 -*-
"""盘后对账（持仓数量 本地 vs 券商，偏差超阈值钉钉告警）。

模块定位（Karpathy 极简原则）：
- 本模块是「调度 + 偏差告警」的薄封装，对账纯逻辑在 trading.execution_gateway.reconcile()
  纯函数里（已由 tests/trading/test_circuit_breaker.py 等覆盖）。
- 我们**不重复造对账逻辑**——BaseExecutionGateway.sync_positions 模板方法已固化
  「拉券商 → reconcile → 返结构化差异」的算法骨架，唯一变化点 _fetch_broker_positions
  由具体子类（QMT/EMT/Mock）实现。本 job 只做：调 sync_positions + 按结果决策告警。

物理意图（盘后 15:30 post_close 触发，二期引擎 Task 7）：
- drifted（数量漂移）：本地记 100 股、券商只记 90——最危险，敞口直接失真，
  可能是部分成交未回写、回调丢消息或断线期间漏单。
- only_local（本地有、券商无）：疑似订单未真正成交或丢单（网络超时后本地乐观
  记账），策略会高估持仓、超额下单。
- only_broker（券商有、本地无）：疑似外部手工成交或另一进程下单，本地对真实
  敞口一无所知，可能与之外部单反向操作。
- is_ok 已综合上述三类（= not drifted and not only_local and not only_broker），
  故偏差判定必须用 not rec.is_ok，**不可只看 only_***——否则会漏掉 drifted，
  让最危险的敞口失真类漂移静默无告警。
"""
from __future__ import annotations

import logging
from typing import Mapping

from trading.execution_gateway import PositionDrift, ReconciliationResult

logger = logging.getLogger(__name__)


def _has_drift(rec: ReconciliationResult) -> bool:
    """对账结果是否有偏差。

    Why 必须用 not rec.is_ok 而非只看 only_*：
    ReconciliationResult.is_ok 的定义是 `not drifted and not only_local and not
    only_broker`——它已综合三类异常。若只看 only_local/only_broker 会**漏掉 drifted**
    （数量漂移，实盘最危险的偏差类：本地以为成交 100 股、券商只记 90，敞口失真）。
    用 not rec.is_ok 既不漏 drifted，也不引入额外语义分叉，保持「单一真理源」。
    """
    return not rec.is_ok


def _format_drift_section(name_cn: str, drifts: list[PositionDrift]) -> str:
    """把一组 PositionDrift 格式化成可读中文段。

    Why 不直接 str(list)：PositionDrift 是 frozen dataclass，默认 repr 是英文
    'PositionDrift(symbol=..., local_qty=...)'——手机端钉钉推送给研究员根本
    看不懂。必须中文标明 symbol/本地/券商/偏差，便于一眼定位风险标的。
    """
    if not drifts:
        return ""
    # 每行一个标的，格式「symbol 本地X/券商Y 偏差Z」，用分号连接控制消息长度。
    items = [
        f"{d.symbol} 本地{d.local_qty:g}/券商{d.broker_qty:g} 偏差{d.delta:+g}"
        for d in drifts
    ]
    return f"{name_cn}（{len(drifts)}）: " + "; ".join(items)


def _build_alert_message(rec: ReconciliationResult) -> str:
    """组装盘后对账偏差告警消息（中文、可读、含三类分类汇总）。"""
    # 三类异常按实盘风险烈度排序：drifted > only_local > only_broker
    s_drifted = _format_drift_section("数量漂移(最危险·敞口失真)", rec.drifted)
    s_only_local = _format_drift_section("本地有券商无(疑似未成交/丢单)", rec.only_local)
    s_only_broker = _format_drift_section("券商有本地无(疑似外部单)", rec.only_broker)
    sections = [s for s in (s_drifted, s_only_local, s_only_broker) if s]

    header = (
        f"【盘后对账偏差】max_abs_drift={rec.max_abs_drift:g}；"
        f"drifted={len(rec.drifted)} only_local={len(rec.only_local)} "
        f"only_broker={len(rec.only_broker)}"
    )
    return header + " | " + " || ".join(sections)


def _alert_drift(msg: str) -> None:
    """触发偏差告警（钉钉 WARN 级），异常被吞。

    level 选 "WARN"：infra/notifier._LEVEL_PREFIX 合法值之一（INFO/WARN/ERROR/
    CRITICAL，见 RiskLevel Literal）；对账偏差属警告级——既不像 INFO 那样被淹没，
    也不像 ERROR/CRITICAL 那样触发误紧急响应（真实灾难级留给熔断/断线）。

    Why notifier 失败不抛：
    对账是盘后风控关键产物，告警通道挂了（钉钉限流/网络抖动）绝不应导致
    整个对账 job 抛异常——否则 scheduler 会因告警侧故障丢对账结果，风险
    敞口彻底失明。fire-and-forget 包 try-except 吞异常，与 qmt_gateway.
    _on_disconnect_fatal 同模式。

    Why 函数级 import（不抽模块级哨兵）：
    原实现用模块级 ``fire_and_forget = None`` + ``_ensure_notifier_loaded``
    双别名哨兵仅为便利测试 monkeypatch，属过度设计（违反 Karpathy 反魔法硬
    约束）。此处与 qmt_gateway._on_disconnect_fatal（见 qmt_gateway.py:530-573）
    同模式：函数内直接 ``from core.notifier import ...`` 内联，零抽象。
    测试通过 monkeypatch 真实模块 ``core.notifier.fire_and_forget`` /
    ``core.notifier.NotificationManager`` 注入 mock（_alert_drift import 时即解析
    到被 patch 的符号），语义完全等价。
    """
    # 函数级 import：规避顶层循环依赖（notifier 初始化可能 import trading）；
    # patch 真实模块 core.notifier.* 后，此处解析到的即 mock，测试可注入。
    from core.notifier import NotificationManager, fire_and_forget
    try:
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "WARN"))
    except Exception:
        logger.exception("盘后对账告警推送失败（已吞，不影响对账结果）")


async def run_reconcile(
    gw,
    local_positions: Mapping[str, float],
    tolerance: float = 0.0,
) -> ReconciliationResult:
    """跑盘后对账 + 偏差超阈值钉钉告警。

    参数：
        gw: BaseExecutionGateway 子类实例（含 sync_positions 模板方法）。
        local_positions: 本地系统记录的理论持仓 {symbol: qty}。
        tolerance: 持仓偏差容忍度（默认 0=零容忍；>0 仅容忍碎股/手续费舍入微差，
                   严禁滥用为掩盖 drift 的借口）。

    返回：ReconciliationResult（供上层 scheduler/看板进一步决策）。
    """
    # 1) 跑对账（算法骨架在 BaseExecutionGateway.sync_positions，已就绪）。
    rec = await gw.sync_positions(local_positions, tolerance=tolerance)

    # 2) 按结果决策告警。
    if _has_drift(rec):
        msg = _build_alert_message(rec)
        logger.warning("盘后对账发现偏差：%s", msg)
        _alert_drift(msg)
    else:
        logger.info("盘后对账无漂移 ✅ max_abs_drift=%s", rec.max_abs_drift)

    return rec
