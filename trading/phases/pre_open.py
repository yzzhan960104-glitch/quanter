# -*- coding: utf-8 -*-
"""trading.phases.pre_open — T 日开盘前挂单（集群 E · 撤昨日单 + 确认闸 + 注入白名单 + 逐单挂单）。

物理定位（T1 模块化拆分 Task 6 · 集群 E）：
    本模块承载盘前挂单域的两个符号（原 ``trading/engine.py`` 模块级函数，逐字搬移 · 行为零变更）：
    - ``pre_open(date, ports)``：C-8 V3 台账包裹（begin/finish + skipped/done/failed 语义判定），
      由 APScheduler cron（``TradingEngine._pre_open`` wrapper）与启动补跑（``trading.catchup``）
      共用，台账在此统一落——「谁先完成谁生效」防双跑。
    - ``_pre_open_impl(date, ports)``：盘前挂单主逻辑 ~330 行（确认闸→撤昨日→熔断基线→平超期→
      注入白名单→逐单挂单 + DB 幂等 + L1/L2 错误分级），是原 engine 最大单函数。

    ``TradingEngine._pre_open`` 实例 wrapper（APScheduler 绑定 · ``@_critical_guard``）**留 engine**
    （spec §2 深剖：wrapper 形态是解耦伏笔），内部改调 ``await pre_open(self._ports, date)``。

迁出纪律（strangler 红线①）：
    函数逻辑【零改动】，只搬位置（trading/engine.py → trading/phases/pre_open.py）。盘前挂单
    时序逐行原样——确认闸（DB per-trade CONFIRMED）→撤昨日未成交→抓日内熔断基线→平超期持仓→
    注入动态白名单→逐单挂单（DB 幂等 has_order(OPEN) + insert_order PENDING + 回填 SUBMITTED/REJECTED）。
    L1/L2 错误分级（_CriticalHalt 停调度 vs L2 聚合 CRITICAL）不变。

================================================================================
模块级符号依赖设计（W1-A/T2 收口后 · 保行为等价 + 测试全绿）
================================================================================
盘前挂单路径是测试 patch 最密集的域（台账语义 / cancel account_id / L1 halt / L2 聚合 / e2e /
engine alerts 等）。

W1-A/T2 反查切断收口语义：原 phases 经函数内 lazy ``import trading.engine as`` 反查 engine
模块级符号的设计**已全量退役**——所有符号改为顶部直接 import 物理真身模块。``patch(
"trading.engine._xxx")`` / ``monkeypatch.setattr(engine, "_xxx", ...)`` 类测试因 pre_open
不再经 engine 模块属性解析而失效，Task 8-19 将这些 patch 迁到物理真身模块路径。各符号现行
依赖如下：

顶部直接 import（物理真身 · 无循环 · 共享模块对象属性级 patch 仍命中）：
    - ``get_gateway`` / ``_submit``：**W1-A/T2-Task7 已切**顶部直接 import gateway_service 真身
      （原 engine re-export 反查 · patch engine.get_gateway / engine._submit 失效 → Task 8-19
      迁 monkeypatch trading.phases.pre_open.get_gateway / trading.phases.pre_open._submit——因本文件
      ``from…import`` 为本地绑定，patch gateway_service 只改模块属性、不命中本地引用，须 patch 调用方）。
    - ``_mode`` / ``_alert_critical``：**W1-A/T2-Task4 已切**顶部直接 import critical 真身
      （patch engine._mode / engine._alert_critical 失效 → Task 8-19 迁 patch critical._mode /
      critical._alert_critical）。
    - ``_resolve_account_id``：**W1-A/T2-Task5 已切**顶部直接 import trading.account SSoT 真身
      （account 是叶子模块无环 · patch engine._resolve_account_id 失效 → Task 8-19 迁 monkeypatch
      account.resolve_account_id 或 setenv QMT_ACCOUNT_ID）。
    - ``_scan_expired_positions`` / ``_close_expired_positions``：**W1-A/T2-Task6 已切**顶部直接
      import phases.stop_loss 真身（同包无环 · patch engine._scan_expired_positions /
      engine._close_expired_positions 失效 → Task 8-19 迁 monkeypatch stop_loss.scan/close_expired）。
      ``_pre_open_impl`` 同模块直调（pre_open wrapper 直调本模块 _pre_open_impl · monkeypatch
      engine._pre_open_impl 失效 → Task 8-19 迁台账测 patch 物理路径）。
    - ``_state_store``：**W1-A/T2-Task4 已切**顶部直接 import state_store 真身（原 engine re-export
      反查 · 整体 patch engine._state_store 失效，属性级 patch state_store.xxx 仍命中共享模块对象
      → Task 8-19 迁整体 patch 路径）。
    - ``_trading_days_between``：**W1-A/T2-Task4 已切**顶部直接 import compute.stop 真身
      （monkeypatch(engine, "_trading_days_between") 失效 → Task 8-19 迁）。
    - ``_cancel_all_open_orders``：**W1-A/T2-Task4 已切**顶部直接 import io.breaker 真身
      （patch engine._cancel_all_open_orders 失效 → Task 8-19 迁 patch io.breaker.cancel_all_open_orders）。
    - ``clock`` / ``job_ledger`` / ``dynamic_whitelist``（``from trading import ...``）：clock 是
      单一时间源（测试经 ``monkeypatch clock.today/now`` 在共享模块对象上 patch → 命中）；
      job_ledger 是台账真相源（测试用真 DB 验语义，不 patch）；dynamic_whitelist 仅 ports=None
      防御回退分支用（``# pragma: no cover``，测试不触达）。
    - ``_CriticalHalt`` / ``_trade_cfg``（``from trading.critical import ...``）：_CriticalHalt 是
      异常类型（按类身份 catch，不被 patch）；_trade_cfg 是纯 env 参数读函数，pre_open 路径无测试
      patch engine._trade_cfg（与 eod_plan 同结论）。
    - ``EnginePorts``（``from trading.ports import ...``）：窄依赖接口（stdlib-only，无循环）。
    - ``OrderRequest``：函数内 local import（原 engine 同位置，``from trading.compute.types``）。

logger 名硬编码 ``trading.engine``（与 order_state.py / eod_plan.py 同口径）：pre_open 原是 engine
    模块级函数，日志/异常打到 trading.engine logger。迁出后保 logger 名不变 = 观测面等价（运维按
    trading.engine 过滤盘前挂单日志不断 + test_pre_open_* / test_l2 caplog 断言命中）。
"""
from __future__ import annotations

import logging
import os

# 窄依赖接口（Task 1 EnginePorts：gate + 动态白名单注入/清空，纯 stdlib 依赖无循环）。
from trading.ports import EnginePorts
# 项目级单例：clock=单一时间源 / job_ledger=台账真相源 / dynamic_whitelist=ports=None 防御回退分支。
# W1-A/T2-Task4：state_store 反查切断 → 顶部直接 import（底层叶子无环 · 整体 patch
# engine._state_store 失效 → Task 8-19 迁 patch 物理路径）。
from trading import clock, dynamic_whitelist, job_ledger
from trading import position_book as _position_book   # review 修复：⑧ per-symbol max_holding 反查曾引用未导入名（每轮 NameError 退 env）
from trading import state_store as _state_store
# W1-A/T2-Task5：_resolve_account_id 反查切断 → 顶部直接 import trading.account
# SSoT 真身（account 是叶子模块无环 · patch engine._resolve_account_id 失效 → Task 8-19 迁）。
from trading.account import resolve_account_id as _resolve_account_id
# _CriticalHalt（L1 致命停调度异常，按类身份 catch 不被 patch）+ _trade_cfg（纯 env 参数读，
# pre_open 路径无 patch engine._trade_cfg 测试）。critical 是 SSoT 基础设施域，不反向 import 本文件。
# W1-A/T2-Task4：_mode / _alert_critical 反查切断 → 同 critical 顶部直接 import
# （patch engine._mode / engine._alert_critical 失效 → Task 8-19 迁 monkeypatch critical._mode 等）。
from trading.critical import _CriticalHalt, _trade_cfg, _mode, _alert_critical
# W1-A/T2-Task4：_trading_days_between 反查切断 → 顶部直接 import compute.stop 真身
# （monkeypatch(engine, "_trading_days_between") 失效 → Task 8-19 迁）。
from trading.compute.stop import trading_days_between as _trading_days_between
# W1-A/T2-Task4：_cancel_all_open_orders 反查切断 → 顶部直接 import io.breaker 真身
# （patch engine._cancel_all_open_orders 失效 → Task 8-19 迁 patch io.breaker.cancel_all_open_orders）。
from trading.io.breaker import cancel_all_open_orders as _cancel_all_open_orders
# W1-A/T2-Task6：_scan_expired_positions / _close_expired_positions 反查切断 → 顶部
# 直接 import phases.stop_loss 真身（同包 phases · stop_loss 模块级不反向 import 本文件 · 无环 ·
# 别名保局部名 _scan/_close_expired_positions · patch engine._scan_expired_positions /
# engine._close_expired_positions 失效 → Task 8-19 迁 monkeypatch stop_loss.scan/close_expired）。
from trading.phases.stop_loss import (
    scan_expired_positions as _scan_expired_positions,
    close_expired_positions as _close_expired_positions,
)
# W1-A/T2-Task7：get_gateway / _submit 反查切断 → 顶部直接 import gateway_service 真身（原 engine
# re-export 反查 · gateway_service 不反向 import 本文件 · 无环 · patch engine.get_gateway /
# engine._submit 失效 → Task 8-19 迁 monkeypatch trading.phases.pre_open.get_gateway / _submit）。
from trading.gateway_service import get_gateway, _submit

# logger 名硬编码 trading.engine（而非 __name__=trading.phases.pre_open）：pre_open 原是 engine
# 模块级函数，日志打到 trading.engine logger。迁出后保 logger 名不变 = 观测面等价（运维按
# trading.engine 过滤盘前挂单日志不断 + test_pre_open_* / test_l2 等 caplog 断言命中）。
logger = logging.getLogger("trading.engine")


# ============================================================================
# 触发点 2：pre_open —— T 日开盘前：撤昨日单 + 检查确认闸 + 挂当日买单
# ============================================================================
async def pre_open(date: str, ports: EnginePorts | None = None) -> dict:
    """T 日开盘前入口（C-8 V3 台账包裹）：running → done/skipped/failed。

    物理意图（spec §3.4）：cron（engine._pre_open）与启动补跑（trading.catchup）共用
    本函数，台账在此统一落（begin/finish）——「谁先完成谁生效」防双跑；
    skipped（gate 未过/无计划/未确认）不算完成，补跑窗口内可重试。
    实现 = 薄包裹 + 原逻辑改名 _pre_open_impl（行为零变更）。

    Args:
        date: T 日（如 "2026-07-22"）。
        ports: T1 缝合点 #1——engine 实例特有依赖（gate + 动态白名单注入/清空）。
            cron wrapper / catchup 补跑传 ``self._ports``；未传（None）走防御分支
            跳过三段闸（与原单例为 None 的防御分支等价）。
    """
    # T1-Task6 → W1-A/T2-Task6 收口：_pre_open_impl 同模块直调（本模块定义、无 import 需要）。
    # 历史 engine 反查为保 monkeypatch.setattr(engine, "_pre_open_impl", fake_impl) 命中
    # test_pre_open_ledger_semantics 台账语义；现同模块直调，monkeypatch engine._pre_open_impl
    # 失效 → Task 8-19 迁（改 monkeypatch pre_open._pre_open_impl 或重构台账测直接调 _pre_open_impl）。
    #
    # 并发硬化（G6 评审结论 · 2026-08-14）：cron 与 catchup 双入口共用本函数，但**刻意不加
    # asyncio.Lock**——三层已闭环并发双挂单，Lock 属冗余且有害：
    #   ① begin_run（G4 BEGIN IMMEDIATE 原子领取——谁先 claim 谁跑，catchup 另含 latest_status
    #      预检跳过 running/done）；
    #   ② DB UNIQUE(account,date,symbol,purpose) + has_order 滤活态；
    #   ③ _pre_open_impl 内 insert_order 返 False→中止 _submit（竞争的第二入口撞 UNIQUE 即停，
    #      绝不脱节下柜台——这是「本项真实收益」）。
    # 且 has_order→insert_order 是同步段（无 await），单事件循环下不可交错；模块级 asyncio.Lock
    # 反会破坏本仓库 asyncio.run(per-test) 跨循环模式。防未来引入 await/--workers>1 时再评估。
    try:
        job_ledger.begin_run("pre_open", date, clock.now().isoformat())
    except Exception:
        logger.exception("job_ledger begin_run 失败（不阻断 pre_open）")
    try:
        result = await _pre_open_impl(date, ports)
    except Exception:
        try:
            job_ledger.finish_run("pre_open", date, "failed", "未预期异常")
        except Exception:
            logger.exception("job_ledger finish_run 失败（不阻断 pre_open）")
        raise
    # A2（08-05 废单根治）：台账不能再拿 done 掩盖 0 成交。
    #   skipped = gate 未过（无计划/未确认/网关/数据未就绪）→ 窗口内可重试；
    #   done    = submitted>0（至少挂出去一张，重试有 has_order 幂等兜底，无需再跑）；
    #   failed  = live 有计划单但 submitted=0（全部被拒/网关全拒）→ C-8 窗口内自动重试。
    if result.get("skipped") or result.get("reason"):
        status = "skipped"
        message = str(result.get("skipped") or result.get("reason") or "")
    elif result.get("submitted", 0) > 0:
        status = "done"
        message = ""
    elif (result.get("mode") == "live" and result.get("submitted", 0) == 0
          and result.get("rejected", 0) > 0):
        # Why failed 而非 done：done 会被 catchup._catchup_pre_open 跳过（status in
        # ("running","done")），08-05 09:22 全拒后 09:30 本可补挂却因 done 永久关闭。
        # failed 不在跳过集 → C-8 窗口 [09:22,10:00) 内自动重试（has_order OPEN 幂等防重复挂）。
        # Why 必须 rejected>0（code-review 修复）：veto/超期/has_order 已挂等「有意跳过」
        # 不是失败（submitted=0 且 rejected=0），记 failed 会误触发 C-8 重试噪音。
        status = "failed"
        message = f"submitted=0/{result.get('total')} rejected={result.get('rejected')}"
    else:
        status = "done"
        message = ""
    try:
        job_ledger.finish_run("pre_open", date, status, message)
    except Exception:
        logger.exception("job_ledger finish_run 失败（不阻断 pre_open）")
    return result


async def _pre_open_impl(date: str, ports: EnginePorts | None = None) -> dict:
    """T 日开盘前：读已确认计划 → 撤昨日遗留未成交单 → 注入白名单 → 逐单挂单。

    物理意图与时序（顺序不可调，与代码实际执行顺序一致）：
        ① 确认闸检查（spec §2 红线）：未确认 → 一律不挂，返「计划未确认」。
           **必须最先做**——确认闸未通过即不应触达任何网关写操作（含撤昨日单），
           否则会误撤昨日已确认单（研究员当日已审核，机器无权撤）。
        ② 撤昨日未成交（scope #2）：避免昨日挂单与新计划叠加导致超额成交。
           仅在 ① 确认闸通过后才撤，避免误撤昨日已确认单。
        ③ 注入动态白名单（Task5）：让当日计划标过关5，但仅在本 engine 进程内生效
           （独立进程不变量，见模块 docstring）。
        ④ 逐单挂单 + try-except 兜底（scope #7）：单标的挡板命中 raise 不炸整批。

    Args:
        date: T 日（如 "2026-07-22"）。

    Returns:
        {"submitted":<成功挂单数>, "mode":..., "reason"?:...}。

    ⚠️ gw=None 行为诚实说明（I3）：
        - **dry_run 模式**：gw=None 仍可继续挂单——submit_order 内部命中 dry_run
          分支返 ``{"state":"DRY_RUN"}`` 不触达 gw，submitted 计数正常（影子观测用）。
        - **live 模式**：gw=None 时 submit_order 会 ``raise RuntimeError``（缺网关），
          被下方逐单 try-except 吞掉，**submitted=0**（全部失败）。
        - **结论**：live 部署前**必须**确保 gateway 已连接（``get_gateway()`` 返非 None），
          否则当日计划一支也挂不上。
    """
    # T1-Task6 → W1-A/T2 收口：engine 模块级符号经 engine 反查的设计已全量退役，所有符号
    # 改顶部直接 import 物理真身（gateway_service.get_gateway/_submit / state_store / critical /
    # compute.stop / io.breaker / account / phases.stop_loss）。patch engine._xxx 失效 →
    # Task 8-19 迁 patch 物理路径（详见模块 docstring「模块级符号依赖设计」）。
    # clock / _trade_cfg / _CriticalHalt / dynamic_whitelist 顶部直接 import（共享模块对象属性 patch 命中）。

    # S3（Task 8 · C-2）：三段式前置 gate（经 EnginePorts.gate 调用实例方法 · T1 缝合点 #1）。
    # 物理意图：plan-confirmed → gateway-health → data-ready 三段全绿才放行下游（撤昨日单 /
    # 抓熔断基线 / 挂新单）。任一未绿即早返 skip payload，绝不触达网关写操作。顺序「先便宜
    # 后贵」（JSON < 探测 < DB 查询）。gate 失败在 live 模式下推 CRITICAL 钉钉（复用
    # _alert_critical 统一收口，与 M4 静默漏单告警同通道）。
    # Why 经 ports.gate：本函数是模块级函数，gate 是 TradingEngine 实例方法（需
    # ``self._plan_data_keys`` 反查策略数据集），T1 前经模块级活跃引擎单例桥反查，
    # T1 起改经 EnginePorts 显式注入（cron wrapper / catchup 传 self._ports）。
    # ports is None 仅在「未装配 engine 的裸调」（外部测试 / 理论不会的生产路径）时发生，
    # 此防御性分支跳过 gate 直接走原 plan["confirmed"] 检查（向后兼容，不破坏旧行为）。
    if ports is not None:
        gate_ok, gate_reason = await ports.gate(date, get_gateway())
        if not gate_ok:
            msg = f"pre_open gate 未通过：{gate_reason}，跳过挂单"
            logger.warning(msg)
            if _mode() == "live":
                # live 模式 gate 拦截 = 当日废单日风险（网关锁死 / 数据未就绪 / 计划未确认），
                # 仅 logger.warning 不足以叫醒用户（spec M4 教训），推 CRITICAL 钉钉。
                _alert_critical(msg)
            # 返回 shape 与 pre_open 其它返回对齐（success: {"submitted","mode"}；
            # skip: {"submitted","reason"}）——保留 skipped（携带 gate reason，比 reason 更
            # 富信息）+ 补 submitted/mode 让任何读 result["submitted"] 的调用方不 KeyError。
            return {"submitted": 0, "mode": _mode(), "skipped": gate_reason}

    # SSoT Phase C · C2c：pre_open 直接读 DB trade_event(SIGNAL).meta（真相源），
    # 不再依赖 plan_*.json。每 SIGNAL 行的 meta 是「精确 per-symbol 计划参数」快照
    # （stop_price/take_profit/neckline/atr/formed_at/max_wait/cancel_on/order/tp1 等），
    # 由 eod_plan 落盘（engine.py:643）。致命日期轴：按 substr(trade_id,-10)=date 查
    # （非 timestamp，timestamp=T 日盘后写入日 ≠ T+1 计划日）。
    account_id_pre = _resolve_account_id()
    signals = _state_store.list_signals_with_meta_by_plan_date(date)
    if not signals:
        # DB 无 SIGNAL → 当日无扫描/无计划，保守不挂（spec 红线）。
        return {"submitted": 0, "reason": "无计划"}
    # 确认闸（DB per-trade CONFIRMED）：研究员人审的「确认」（生产路径已废，仅测试 legacy）
    # 或 eod_plan auto 路径（AUTO_CONFIRM_PLAN=true）写 trade_event CONFIRMED 行。
    # 任一 SIGNAL 的 latest_action 非 CONFIRMED（仅 SIGNAL/VETOED/未确认）→ 整体未确认。
    # 物理意图（spec 红线）：宁可漏挂，不挂研究员未审核的单。
    # Why 整体判断而非逐笔：原 plan["confirmed"] 是整张计划级布尔（研究员一次性确认全部），
    # DB 化后 per-trade CONFIRMED 但语义保持「全部确认才挂」——若部分未确认而挂已确认部分，
    # 会破坏研究员「整张计划审核」的工作流。逐笔 CONFIRMED 校验在下方循环内再防一次
    # （VETOED per-symbol 跳过）。
    all_confirmed = True
    for sig in signals:
        _tid = _state_store.build_trade_id(account_id_pre, sig["symbol"], date)
        # **ssot-review P1 fix**：原严格 !=CONFIRMED 在部分标的已挂单（ORDERED）后判
        # 未确认 → pre_open 重入时剩余标的永不补挂（live 红线）。改用 is_trade_confirmed
        # 单点（CONFIRMED + ORDERED/FILLED/CLOSED/TP_FILLED 均视作已确认，与 _stoploss /
        # trading_plan.load_plan 三处语义单点对齐）。VETOED 仍视作未确认（veto 终局，
        # is_trade_confirmed 返 False → all_confirmed=False → 不放行）。
        if not _state_store.is_trade_confirmed(_tid):
            all_confirmed = False
            break
    if not all_confirmed:
        return {"submitted": 0, "reason": "计划未确认，跳过挂单"}

    # ② 撤昨日未成交（scope #2）：仅在确认闸（①）通过后才撤，避免误撤昨日已确认单。
    gw = get_gateway()
    if gw is None:
        # gw 未装配：影子模式仍可挂 DRY_RUN（dry_run 命中不触达 gw）；真单模式下
        # 挂单也会因 gw=None 抛 RuntimeError 由下方 try-except 吞掉，故这里只 warning。
        logger.warning("pre_open 撤昨日单跳过：交易网关未装配（gw=None）")
    else:
        try:
            # M2（T3）：cancel_all_open_orders 返回 {cancelled, unconfirmed}。
            # cancelled=成功发起撤单数；unconfirmed=发起后 _confirm_cancelled 超时
            # 未到终态的笔数（主推延迟/柜台未响应）。unconfirmed>0 仅告警不阻塞挂单——
            # 撤单已发，本地状态终会被 on_cancel_error/on_stock_order 对账修正，但必须
            # 显式暴露此口径让运维知晓，杜绝「本地以为撤了、柜台其实没撤」的状态悬空。
            # C-4 U5：补传 account_id 激活柜台路径 cancel_order_by_broker_oid_db 回写
            # order.state=CANCELLED（breaker._cancel_via_broker_query 在 account_id 提供时才回写）。
            # Why 必传：不传则撤了昨日单 DB 仍记 SUBMITTED → T+1 对账幽灵单（spec §6.1 判据）。
            # 此为 C-3 审计结论的最小修（无需 purpose='CANCEL' 行，既有回写路径已够）。
            _cancel_res = await _cancel_all_open_orders(gw, account_id=_resolve_account_id())
            n_cancelled = _cancel_res["cancelled"]
            n_unconfirmed = _cancel_res["unconfirmed"]
            logger.info(
                "pre_open 撤昨日未成交单 发起 %s 笔（未确认 %s 笔）",
                n_cancelled, n_unconfirmed,
            )
            if n_unconfirmed > 0:
                logger.warning(
                    "pre_open 有 %s 笔撤单未确认终态（主推延迟或柜台未响应，需人工复核）",
                    n_unconfirmed,
                )
        except Exception:
            # 撤单失败不阻塞挂单主路径（单笔失败已在 cancel_all 内被吞，此处兜整体异常）
            logger.exception("pre_open 撤昨日单整体异常（继续挂新单）")

    # ②.5 抓日内熔断基线 + account_daily start 快照（W4 · 08-04 断链根治 · spec §5.2）：
    # 物理意图：post_close 判 -3% 熔断需要 start_equity 基线，开盘前是唯一可靠的
    # 「未受当日交易影响」时点。pre_open 在确认闸 + 撤昨日单后调 gw.query_asset 抓当日
    # 开盘总资产 → 写 **account_daily** 表的 start 字段。
    #
    # W4 断链根治（08-04 发现 2）：原调 ``_position_book.snapshot_start_equity`` 写
    # **daily_equity** 表，而 post_close 的 ``snapshot_close_equity`` 写 **account_daily**
    # 表并读同表 ``start_total_asset`` 算 ``daily_pnl = close - start``——两表断链致
    # account_daily.start_total_asset 恒为 NULL → daily_pnl 恒 NULL（start 落在 daily_equity，
    # post_close 在 account_daily 找不到）。改调 ``_state_store.snapshot_start_equity``
    # 写 account_daily.start，与 post_close 同表 → daily_pnl 闭合。
    #
    # daily_equity 表读口已迁（C-1 收口 · 08-04 Task 9 review 根因）：原熔断读口
    # ``_position_book.get_start_equity``（读 daily_equity 表）在 W4 后恒返 None（daily_equity
    # 再无生产写入方）→ 日内熔断永久失效。post_close 步骤1 已改读
    # ``_state_store.get_start_equity``（与 pre_open 写口同表 account_daily），熔断基线闭合。
    # B5（SSoT Phase B）已删 position_book.snapshot/get_start 函数 + daily_equity DDL，
    # 熔断基线唯一读口 = state_store.get_start_equity(account_daily)。旧库残留 daily_equity
    # 表无害（init 不再 CREATE，不读写）。
    #
    # 边界（红线）：
    # - gw=None / query_asset 返 {}（未连接/锁定/超时）→ 跳过 + WARN，绝不拿 0/None
    #   写基线（DG-G3 后基线缺失=post_close 走 T-1 兜底→仍无则 None 直传 breaker fail-closed
    #   触发保护；拿 0 写会致 daily_pnl 除零+语义模糊——非旧 fail-open「永不熔断」）；
    # - query_asset 异常 → 跳过 + 告警（不阻塞挂单主路径）。
    # Why 在撤单后而非前：撤单不影响总资产（仅未成交单状态变化），先后顺序无关；
    # 放后面可与撤单共用同一个 gw 引用，且「撤完昨日 → 抓今日基线」语义更清晰。
    # C-6 V2：用传入 date（入口缓存，_pre_open 已算 clock.today 传 pre_open），不重复 datetime.now。
    today_eq = date

    def _notify_baseline_missing(reason: str) -> None:
        """C-1 熔断基线缺失统一告警（warning + live CRITICAL 钉钉）+ DG-G3 T-1 兜底回填。

        pre_open 抓基线失败的三种场景（query_asset 返空 / 抓取异常 / gw=None）共用本
        helper，防 C-1 告警文案漂移 + 确保三处都触发 live CRITICAL。物理意图：基线缺失
        = 日内 -3% 熔断将失效（穿仓风险），开盘前必须叫醒人工（post_close T-1 close
        兜底是第二道防线，但开盘前告警让用户可立即 trigger_pre_open_once 补精确基线）。

        DG-G3（2026-08-13）T-1 兜底回填（红线「fail-closed + 基线兜底同 commit」）：
            breaker fail-closed 后若 pre_open 不兜底，则 query_asset 失败的每个交易日
            都会让 post_close → breaker fail-closed 停手 = **每天开盘误熔断中断业务**。
            本 helper 在告警后追加：用 T-1 close 写 account_daily.start（隔夜无交易近似），
            让 post_close 读到非 None 基线 → 熔断正常判定，fail-closed 仅在 T-1 close 也无时触发。
            与 post_close 本地 T-1 兜底互补——这里写 DB 真相源（所有读口统一受益，SSoT）。
        """
        msg = f"pre_open 跳过熔断基线快照：{reason} date={today_eq}"
        logger.warning(msg)
        if _mode() == "live":
            _alert_critical(
                msg + "（C-1 熔断基线缺失，post_close 将用 T-1 close 近似；"
                "可手动 trigger_pre_open_once 补精确开盘基线）")
        # DG-G3：精确抓失败后用 T-1 close 兜底回填 account_daily.start（防每天误熔断中断业务）
        _backfill_start_from_t1_close(today_eq, reason)

    def _backfill_start_from_t1_close(today_eq: str, miss_reason: str) -> None:
        """DG-G3 兜底：精确基线抓取失败 → 用 T-1 close 写 account_daily.start 近似基线。

        物理意图（DG-G3 红线 · spec audit-remediation §G3）：
            隔夜无交易 T-1 收盘 ≈ T 开盘，T-1 close 是次精确的开盘基线近似。写回 DB
            account_daily.start 后，post_close 与所有读口统一拿到非 None 近似基线，
            熔断正常工作（不必触发 fail-closed 停手）。

        边界（防御性深度）：
            - T-1 close 也无（连续异常日 / 首个交易日 / 日历未就绪）→ **不写脏值**，
              account_daily.start 保留 NULL，让 post_close → breaker fail-closed 处理
              （绝不拿 0 写基线，防 post_close 读到 0 触发除零或语义模糊）；
            - snapshot_start_equity 写入异常 → logger.exception（不阻塞挂单主路径，
              与精确抓取的 except 同口径）。
        """
        try:
            prev_close = _state_store.get_prev_close_equity(
                _resolve_account_id(), today_eq)
        except Exception:
            logger.exception(
                "pre_open T-1 close 兜底读取异常 date=%s（保留 account_daily.start NULL）",
                today_eq)
            return
        # 类型防御：get_prev_close_equity 契约返 float|None，但 mock 环境（如本引擎其他单测
        # 把 state_store 整体 monkeypatch 成 MagicMock）会返 MagicMock。fail-closed 语义下，
        # 非数值视作「无效兜底」安全降级（保留 None 让 breaker fail-closed），绝不让类型
        # 异常 TypeError 中断 pre_open 主路径。
        if not isinstance(prev_close, (int, float)) or prev_close <= 0:
            # T-1 close 也无 / 类型异常 = 基线链全失效 → 不写脏值，让 breaker fail-closed 处理
            logger.warning(
                "pre_open T-1 close 也无（基线链全失效）date=%s"
                "（account_daily.start 保留 NULL，breaker 将 fail-closed 触发保护）", today_eq)
            return
        try:
            _state_store.snapshot_start_equity(
                _resolve_account_id(), today_eq, float(prev_close))
            logger.info(
                "pre_open T-1 close=%s 回填 account_daily.start date=%s"
                "（精确基线抓取失败：%s）", prev_close, today_eq, miss_reason)
        except Exception:
            logger.exception(
                "pre_open T-1 close 回填 account_daily.start 异常 date=%s", today_eq)

    if gw is not None:
        try:
            asset = await gw.query_asset() if hasattr(gw, "query_asset") else {}
            total = (asset or {}).get("total_asset")
            if total is not None and float(total) > 0:
                # W4：cash 取 asset dict 的 cash 字段（与 post_close snapshot_close_equity
                # 的 close_cash 同口径，gw.query_asset 返 {total_asset, cash, market_value}）。
                cash = (asset or {}).get("cash")
                _state_store.snapshot_start_equity(
                    _resolve_account_id(), today_eq, float(total),
                    float(cash) if cash is not None else None)
                logger.info("pre_open 日内熔断基线已抓取 date=%s start_equity=%s",
                            today_eq, float(total))
            else:
                # query_asset 返空（未连接/锁定/超时）→ 告警 + DG-G3 T-1 close 兜底回填
                _notify_baseline_missing("query_asset 返空（未连接/锁定/超时）")
        except Exception:
            # 抓基线异常（超时/锁定报错的真实落点）→ 记 traceback + 告警（spec 三处站点之一）
            logger.exception("pre_open 抓熔断基线异常（不阻塞挂单主路径） date=%s", today_eq)
            _notify_baseline_missing("query_asset 抓取异常（见上方 traceback）")
    else:
        _notify_baseline_missing("gw=None（网关未装配）")

    # ②.6 Task 8（P0-4 max_holding）：平超期持仓（跌停价释放资金）。
    # 物理意图：回测 max_holding 超时平仓对齐——持仓超 max_holding 日未达止盈即收盘平，
    # 实盘在【pre_open】挂跌停价卖单（保证成交，接受滑点；超时释放资金不等好价位）。
    # Why 在挂新买单前：先释放超期资金占用再挂新单（资金可用度更准）；卖单与买单方向
    # 相反不冲突。qty 必须来自 gw 真实持仓（红线，同 stop_loss_monitor，绝不硬编码）。
    #
    # SSoT Phase B 断点-2（B1 · pre_open 现算，删 expired_positions.json 跨日传递）：
    # 基准日 = clock.pretrade_date(clock.today()) = 上一交易日（T-1 收盘口径），非 today。
    # Why 不用 today：pre_open 在 T 日开盘前跑，T 日未收盘，entry_date 与 T 日做差会把
    # T 日算入 holding_days（多算一日）→ 超期判定整体提前 → 误平窗口内持仓（致命）。
    # Why 现算非读文件：post_close 写盘 + pre_open 读盘是断点-2 前的双写镜像（CSV 形态），
    # 文件覆盖写 + 跨日传递有竞态/崩溃丢失风险，且 holding_days 计算已可通过 position_book
    # 任意时刻现算（无状态，幂等）→ 删文件函数全收口到 pre_open 单点。
    _asof = clock.pretrade_date(clock.today())  # 基准日=上一交易日（断点-2，零漂移）
    # ⑧ per-symbol max_holding（单源收敛 · 2026-08-17）：新持仓按入场 SIGNAL.meta.
    # exec_params（实验值定终身，如 20）；老持仓（无快照）走 env 缺省（15）自然过渡。
    # 反查失败（DB/账本异常）→ 整体退 int 全局口径（现行为，绝不因反查失败不扫超期）。
    _mh_env = int(_trade_cfg()["max_holding"])
    _mh = _mh_env
    try:
        _entry_syms = list(_position_book.get_entry_dates().keys())
        if _entry_syms:
            _mh_map = {s: _mh_env for s in _entry_syms}   # env 缺省打底
            for _m in _state_store.list_active_holding_signals(
                    _resolve_account_id(), _entry_syms):
                _ep = _m.get("exec_params")
                if isinstance(_ep, dict) and isinstance(_ep.get("max_holding"), (int, float)):
                    _mh_map[_m["symbol"]] = int(_ep["max_holding"])
            _mh = _mh_map
    except Exception:
        logger.exception("pre_open per-symbol max_holding 反查失败，退 env 全局口径 %d", _mh_env)
    _expired = _scan_expired_positions(_asof, _mh)
    if _expired:
        await _close_expired_positions(gw, _expired)

    # ③ 注入动态白名单（Task5 → C-2 W1 改实例属性 · T1 经 ports.whitelist_add）：仅 engine 通道生效。
    # 改造后注入到 engine 实例 self._dynamic_whitelist（经 EnginePorts 显式注入，
    # 而非模块级 _DYNAMIC 全局——engine 与 server 合并进同进程后，实例属性化是两端
    # 白名单物理隔离的唯一手段（server 路径不读实例属性））。ports 由 cron wrapper /
    # catchup 传 self._ports，None 守卫为防御性兜底（未装配 engine 的裸调回退模块级全局）。
    # C2c：signals 项 shape = {symbol, **meta}，meta 含 order 子 dict（meta["order"]）
    symbols = {sig["order"]["symbol"] for sig in signals if sig.get("order")}
    if ports is not None:
        ports.whitelist_add(symbols)
    else:  # pragma: no cover - 防御性回退：未注入 ports（未装配 engine 的裸调，理论仅测试）
        dynamic_whitelist.inject_dynamic_whitelist(symbols)

    # ③.5 ADR-16 人工风控双值（2026-08-17，regime 指标闸移除后的增量拦截接管面）：
    #   block_new_orders=1 → 撤昨日单/超期平仓/熔断基线等存量管理已照常执行（上方），
    #     仅挂新买单整体跳过（skip payload + WARN 播报）；
    #   max_total_position<1.0 → 逐单额度检查「持仓市值 + 本轮已挂 + 本单 ≤ 比例×总权益」，
    #     超限单跳过（n_pos_capped 计入返回 payload）。默认 1.0 = 不限制（零行为变化）。
    # resolve 自身 fail-closed：DB 读异常 → block=True 全拦（宁错拦人工拨回，不裸奔）。
    # 类型守卫：非 {block:bool, max_pos:num} 形态（如测试环境整体 MagicMock state_store
    # 时下标返 MagicMock）→ 按默认不拦处理——真 fail-closed 已在 resolve 内部完成，
    # 此处只防调用方注入的脏形态误触发拦截分支。
    _rc_any = _state_store.resolve_risk_control()
    _rc = (_rc_any if isinstance(_rc_any, dict)
           and isinstance(_rc_any.get("block"), bool)
           and isinstance(_rc_any.get("max_pos"), (int, float))
           and not isinstance(_rc_any.get("max_pos"), bool)
           else {"block": False, "max_pos": 1.0, "degraded": True})
    if _rc["block"]:
        _msg = (f"pre_open 人工风控拦截：block_new_orders=1（ADR-16），"
                f"{len(signals)} 单全部跳过挂单（撤昨日单/超期平仓/熔断基线已照常执行）"
                + ("；⚠ risk_control 读取异常 fail-closed，请查 DB" if _rc["degraded"] else ""))
        logger.warning(_msg)
        try:  # WARN 播报软降级（人工开关是已知状态非事故，不占 CRITICAL 通道）
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(
                NotificationManager.get_default().notify_risk_event(_msg, "WARN"))
        except Exception:
            logger.debug("人工风控拦截播报软降级", exc_info=True)
        return {"submitted": 0, "mode": _mode(),
                "skipped": "人工风控开关：拦截增量下单（存量管理已执行）"}

    # 总仓位额度（仅 max_pos<1.0 时启用）：quota = 比例×总权益 − 持仓市值，本轮挂单逐单扣减。
    # Why fail-closed：设置了上限但权益/市值查不到（断线/返空）→ 全跳过——「不知道占多少」
    # 时宁可多拦（人工可 trigger_pre_open_once 补挂），不可盲放。
    _pos_quota: float | None = None   # None = 不限制（max_pos=1.0 默认，零行为变化）
    if _rc["max_pos"] < 1.0:
        _asset_rc: dict = {}
        if gw is not None:
            try:
                _asset_rc = await gw.query_asset() or {}
            except Exception:
                logger.exception("pre_open 总仓位检查 query_asset 异常（fail-closed 全跳过）")
                _asset_rc = {}
        _total_rc = _asset_rc.get("total_asset")
        _mv_rc = _asset_rc.get("market_value")
        # Review 修复（I-2）：额度基数计入未终态买入委托的剩余占额（当日更早挂单/
        # 补挂路径不再击穿上限）；DB 读异常与权益失败同口径 fail-closed（CR-4 同型）。
        try:
            _open_buy = _state_store.get_open_buy_amount(_resolve_account_id())
        except Exception:
            logger.exception("pre_open 总仓位检查 get_open_buy_amount 异常（fail-closed 全跳过）")
            _open_buy = None
        if (_total_rc is None or float(_total_rc) <= 0 or _mv_rc is None
                or _open_buy is None):
            _msg = (f"pre_open 总仓位上限 {_rc['max_pos']:.0%} 生效但权益/委托查询失败"
                    f"（total_asset={_total_rc} market_value={_mv_rc} open_buy={_open_buy}），"
                    f"fail-closed 跳过全部新单")
            logger.warning(_msg)
            if _mode() == "live":
                _alert_critical(_msg)
            # Review 修复（I-1）：必须带 skipped 键——wrapper 五分支据它记台账 skipped
            # （C-8 窗口内可重试）；无键会落 else→done，瞬时故障被永久关闭重试。
            return {"submitted": 0, "rejected": 0, "total": len(signals),
                    "mode": _mode(), "pos_check_failed": True,
                    "skipped": "总仓位检查权益/委托查询失败（fail-closed 全跳过）"}
        _pos_quota = float(_total_rc) * _rc["max_pos"] - float(_mv_rc) - _open_buy
        logger.info("pre_open 总仓位额度：%.0f%%×%.2f−%.2f−%.2f = %.2f（已扣未终态买单，本轮逐单扣减）",
                    _rc["max_pos"], float(_total_rc), float(_mv_rc), _open_buy, _pos_quota)

    # ④ 逐单挂单 + raise 兜底（scope #7）
    from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
    # plan Task 6（P0-2 max_wait 窗口）：pre_open 按 formed_at+max_wait 过滤超期信号。
    # 物理意图：回测信号后 max_wait 天窗口等回踩；实盘原口径只挂 1 天（次日 pre_open 撤昨日），
    # 改为窗口内每日可挂（回测对齐）。窗口外（trading_days > max_wait）的信号视为过期跳过。
    # 边界：order 缺 formed_at → days=0 视窗口内挂单（向后兼容老 plan）；缺 max_wait → 用 _trade_cfg 默认 5。
    # C-6 V2：用传入 date（入口缓存，防同轮跨午夜漂移）。
    today_for_max_wait = date
    cfg_max_wait = int(os.getenv("TRADE_MAX_WAIT", "5"))
    n_submitted = 0
    n_expired = 0
    n_rejected = 0   # C-4 U4：单只业务拒单计数（L2 聚合 CRITICAL 用，防告警风暴）
    n_pos_capped = 0  # ADR-16：总仓位额度超限跳过计数（聚合播报，防逐单刷屏）
    account_id = _resolve_account_id()
    # 确保 account 行存在（insert_order/trade_event FK 引用）
    # C-4 U3a：account 行是 insert_order/trade_event 的 FK 源——get_account/upsert_account
    # 失败=后续所有 DB 写 FK 全失效=DB 真故障。原软降级会让下游连环报 FK 错仍继续挂单，
    # 升 L1（review 补强：基础设施 > 单只计数）。
    try:
        if _state_store.get_account(account_id) is None:
            _state_store.upsert_account(account_id, broker="qmt")
    except Exception as e:
        raise _CriticalHalt(
            f"pre_open 确保 account 行失败 account={account_id}（DB 真故障，下游 FK 全失效）") from e
    for o in signals:
        od = o["order"]
        # max_wait 窗口过滤（plan Task 6）
        formed_at = o.get("formed_at")
        if formed_at:
            order_max_wait = int(o.get("max_wait") or cfg_max_wait)
            days_since = _trading_days_between(formed_at, today_for_max_wait)
            if days_since > order_max_wait:
                n_expired += 1
                logger.info("pre_open 跳过超期信号 symbol=%s formed_at=%s days=%d > max_wait=%d",
                            od["symbol"], formed_at, days_since, order_max_wait)
                continue
        # T8（state-store-redesign §4.1）DB 幂等挂单：
        # ① veto 保护：trade_event 最新 action=VETOED → 跳过（研究员否决不挂）
        # ② has_order(OPEN)：同日同标的已挂过 OPEN → 跳过（pre_open 重跑/崩溃重启不重复挂）
        trade_id = _state_store.build_trade_id(account_id, od['symbol'], date)
        try:
            if _state_store.get_latest_action(trade_id) == "VETOED":
                logger.info("pre_open 跳过 vetoed 标的 symbol=%s", od["symbol"])
                continue
            if _state_store.has_order(account_id, date, od["symbol"], "OPEN"):
                logger.info("pre_open 跳过已挂 OPEN（DB 幂等）symbol=%s", od["symbol"])
                continue
        except Exception as e:
            # C-4 U3a：幂等读失败=「不知是否已挂过」→ 继续挂=可能重复挂（双倍成交，真金损失）。
            # 原 soft-degrade 注释「不阻断，可能重复挂」即承认了真金损失风险——升 L1
            # （spec §3 state_store 关键读失败 = L1）。宁可停整批不盲挂。
            raise _CriticalHalt(
                f"pre_open DB 幂等读失败 symbol={od['symbol']}（敞口未明，拒继续挂）") from e
        # ADR-16 总仓位额度检查（在 DB 幂等读之后、insert_order 之前——被跳过的单不留
        # DB 痕迹，人工提额后 trigger_pre_open_once 补挂即可重走本检查）：
        # 本单金额 > 剩余额度 → 跳过（只拦增量，不撤已挂单）。
        if _pos_quota is not None:
            _order_amt = float(od["qty"]) * float(od["price"])
            if _order_amt > _pos_quota:
                n_pos_capped += 1
                logger.warning(
                    "pre_open 总仓位额度跳过 symbol=%s 金额=%.2f > 剩余 %.2f（上限 %.0f%%）",
                    od["symbol"], _order_amt, _pos_quota, _rc["max_pos"])
                continue
        order_req = OrderRequest(
            symbol=od["symbol"], qty=od["qty"], side=od["side"], price=od["price"],
        )
        # T8：挂单前先 insert_order(OPEN, PENDING)（DB 真相源，幂等 UNIQUE）
        # C-4 U3a：insert_order 是 DB 真相源写入——失败=柜台可能挂了但 DB 没记=对账幽灵单。
        # 原 soft-degrade「不阻断挂单」会让幽灵单在重跑时被当成「未挂」重复挂（双倍成交）。
        # 升 L1（review 补强：单只层面 DB 写异常 > 单只计数，硬抛停调度，绝不带病挂下一只）。
        try:
            _order_id = f"{date}_{od['symbol']}_OPEN_1"
            # G6（spec audit-remediation §G6「本项真实收益」）：捕获 insert_order 返回值——
            # False = UNIQUE 四元组 (account,date,symbol,OPEN) 已占位（多为 REJECTED 等死态残留，
            # has_order 已滤活态放行至此）。绝不可忽略 False 继续 _submit：否则柜台下真单而 DB
            # 无对应 OPEN 行 = 对账幽灵单（spec 点名「DB 幂等拦不住柜台」）。
            # 语义副作用（知情）：死态占位 → 当日不再重挂（C-8 重试窗口内 submitted 仍 0）；spec
            # 已权衡——拒单多因资金/涨跌停，同日重挂无意义，宁可停手不造 DB/柜台脱节的幽灵单。
            _inserted = _state_store.insert_order(
                _order_id, trade_id, account_id, date, od["symbol"], od["side"], "OPEN",
                float(od["qty"]), float(od["price"]), state="PENDING")
        except Exception as e:
            raise _CriticalHalt(
                f"pre_open insert_order(OPEN) 失败 symbol={od['symbol']}（DB 真相源失真）") from e
        if not _inserted:
            # G6：DB 幂等落库失败（slot 占位）→ 中止本只 _submit + 告警 + 计入拒单。
            # 计入 n_rejected 让台账 status=failed（submitted=0/rejected>0）暴露异常，C-8 可感知。
            # 同时顺带消除「REJECTED→SUBMITTED」非法迁移——不 _submit 即不回写 SUBMITTED。
            logger.warning(
                "pre_open 中止 _submit：insert_order 返 False（UNIQUE 占位/死态残留）"
                " symbol=%s order_id=%s（DB 幂等拦柜台·G6）", od["symbol"], _order_id)
            if _mode() == "live":
                _alert_critical(
                    f"pre_open 中止挂单 symbol={od['symbol']}（insert_order 返 False·UNIQUE "
                    f"占位，防 DB/柜台脱节 G6）")
            n_rejected += 1
            continue
        try:
            result = await _submit(order_req)
        except Exception as exc:
            # 挡板命中（资金不足/涨跌停/不在白名单等）会 raise RuntimeError
            # （trading_service.submit_order 契约）——必须逐单吞，一只拒单不炸整批。
            logger.warning("pre_open 挂单失败 symbol=%s 原因=%s", od["symbol"], exc)
            n_rejected += 1   # C-4 U4：聚合 L2 CRITICAL 计数（循环末尾汇总一条，防风暴）
            # C-1 final-review fix (I-2)：失败时把残留 PENDING 行标 REJECTED，否则
            # has_order(OPEN) 恒 True → 重跑永久漏挂（live 资金不足/涨跌停挡板致命）。
            try:
                _state_store.update_order_state(_order_id, "REJECTED")
            except Exception:
                logger.exception("pre_open 失败回填 REJECTED 失败 symbol=%s", od["symbol"])
            continue
        # state 是 OrderState.name 字符串；REJECTED/FAILED 视为未挂成功
        if result.get("state") not in ("REJECTED", "FAILED"):
            n_submitted += 1
            # ADR-16：挂单成功才扣减总仓位额度（拒单/失败单的额度在下轮重挂时仍可用）
            if _pos_quota is not None:
                _pos_quota -= float(od["qty"]) * float(od["price"])
            # T8：挂单成功 → 回填 order.state=SUBMITTED + broker_oid（seq）+ trade_event(ORDERED)
            try:
                broker_oid = str(result.get("order_id") or "")
                _state_store.update_order_state(
                    _order_id, "SUBMITTED",
                    broker_oid=broker_oid or None,
                    submitted_at=clock.now().isoformat())
                _state_store.insert_trade_event(
                    account_id, trade_id, od["symbol"], "ORDERED",
                    order_id=_order_id, qty=float(od["qty"]), price=float(od["price"]))
            except Exception as e:
                # C-4 U3a：柜台挂成功了（_submit 返非 REJECTED/FAILED）但 DB 没回 SUBMITTED
                # = 对账以为没挂 → 幽灵单 / 重跑重复挂。升 L1（DB 真相源失真优先于单只）。
                raise _CriticalHalt(
                    f"pre_open 回填 SUBMITTED/ORDERED 失败 symbol={od['symbol']}（DB 真相源失真）") from e
        else:
            logger.warning("pre_open 挂单未成功 symbol=%s state=%s msg=%s",
                           od["symbol"], result.get("state"), result.get("message"))
            n_rejected += 1   # C-4 U4：未成功（REJECTED/FAILED）也计 L2 聚合（业务拒单同义）
            # C-1 final-review fix (I-2)：未成功（REJECTED/FAILED）标死态，has_order 放行重挂。
            try:
                _dead = "REJECTED" if result.get("state") == "REJECTED" else "FAILED"
                _state_store.update_order_state(_order_id, _dead)
            except Exception:
                logger.exception("pre_open 失败回填 %s 失败 symbol=%s", _dead, od["symbol"])

    logger.info("pre_open 完成 date=%s submitted=%d/%d expired=%d mode=%s",
                date, n_submitted, len(signals), n_expired, _mode())
    # C-4 U4：部分拒单（L2）聚合一条 CRITICAL——单只研究员要知情，但整批继续不炸。
    # Why 聚合非逐只：防 N 只全拒告警风暴（spec R3）。整批 submitted=0 已有下方 CRITICAL（保留）。
    # Why 限 live：dry_run/测试的拒单非真金风险，防误告警。n_submitted>0 守卫：全拒走下方 submitted=0 通道。
    if n_rejected > 0 and _mode() == "live" and n_submitted > 0:
        _alert_critical(
            f"pre_open 部分挂单被拒 rejected={n_rejected}/{len(signals)} "
            f"submitted={n_submitted} date={date}（查挡板日志：涨跌停/资金/白名单）")
    # Task 9（M4 静默漏单消灭）：live 模式 submitted=0 且有计划单 → 钉钉 CRITICAL。
    # 物理意图：live 下「全部挂单失败」= 当日废单日（网关锁死 / 涨跌停挡板 / 资金不足），
    # 仅 logger.warning 不足以叫醒用户（[[qmt-connect-1-rootcause]] 全天锁死无告警教训）。
    # Why 限 live：dry_run submitted=0 多半是 DRY_RUN 状态误判或测试 mock，非真漏单风险；
    # Why 限 len(orders)>0：无计划单（0/0）是「当日无信号」正常态，不该误告警。
    if n_submitted == 0 and _mode() == "live" and len(signals) > 0:
        _alert_critical(
            f"pre_open 漏挂 submitted=0/{len(signals)} date={date}"
            f"（网关锁死? 网关拒绝所有单? 人工核查 gw 状态与挡板日志）")
    # ADR-16：总仓位超限聚合播报（WARN——是人工设限的预期行为，非事故）
    if n_pos_capped > 0:
        _msg = (f"pre_open 总仓位上限 {_rc['max_pos']:.0%}：{n_pos_capped}/{len(signals)} 单"
                f"超限跳过，submitted={n_submitted}（提额可 trigger_pre_open_once 补挂）")
        logger.warning(_msg)
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(
                NotificationManager.get_default().notify_risk_event(_msg, "WARN"))
        except Exception:
            logger.debug("总仓位超限播报软降级", exc_info=True)
    # A2：返回 submitted/rejected/total 供台账判定（submitted=0 且有单 → failed，不再 done 掩盖）。
    return {"submitted": n_submitted, "rejected": n_rejected,
            "total": len(signals), "mode": _mode(), "pos_capped": n_pos_capped}
