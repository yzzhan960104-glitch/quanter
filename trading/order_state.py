"""订单状态机域（OrderStateMachine + broker 订单回调三分支 + 状态推进）。

本文件承载两类「订单状态」语义（T1 模块化拆分 Task 3 后合并于此）：

1. **OrderStateMachine**（Layer2 follow-up #4c 定型 · imperative shell 状态机真身）：
   有状态可变对象，管理订单的合法状态迁移（PENDING → SUBMITTED → FILLED/CANCELLED/...），
   不进 functional core。Layer2 #4c 后本文件「只保留 OrderStateMachine」的状态在 T1 Task 3
   被打破——engine.py 的 broker 订单回调链路同属「订单状态」域，一并迁入。

2. **broker 订单回调三分支 + 状态推进**（T1 Task 3 · 集群 I · 缝合点 #2）：
   ``handle_order_update`` / ``order_direction`` / ``advance_order_state_from_status``
   原 engine.TradingEngine 实例方法（spec §6.2 C1 三连 + #5 第二刀状态推进），处理
   on_stock_trade / on_stock_order / on_order_stock_async_response 推送的成交/委托/异步回报。

设计原则：
- 有限状态机（FSM）模式
- 显式状态迁移（不隐式跳转）
- 防范非法状态迁移

Layer2 follow-up #4c 定型（spec §3.5 · OrderState 纯枚举单源）：
    OrderState 纯枚举单源在 trading/types/order_state.py（``from trading.types.order_state import OrderState``）。
    出场逻辑纯函数（check_stop_loss / check_take_profit / update_trailing_stop）
    单源在 trading/compute/stop.py（``from trading.compute.stop import ...``）。

============================================================================
T1 Task 3 缝合点 #2 设计（brief Step 2 · 临时耦合 engine · Task 9 收口）
============================================================================
3 个 broker 回调函数迁出后改为**接收 engine 实例引用**的 free function：
    ``async def handle_order_update(engine, update)``
    ``def order_direction(engine, order_id)``
    ``def advance_order_state_from_status(engine, update)``
内部访问点逐行原样（幂等红线零容忍）：
- 实例属性 ``self._gw`` → ``engine._gw``（网关实例引用 · 非模块级符号）；
  止盈挂单 T1-Task9 已收口——``place_take_profit`` 顶部直接 import phases.exit 真身（W1-A/T2-Task6
  切断历史 engine re-export 反查），不再经 engine 实例引用（消除缝合点 #2 耦合）。
- engine 模块级符号（``_mode`` / ``_alert_critical`` / ``_resolve_account_id`` /
  ``_seq_for_real_oid`` / ``_order_state_to_db``）顶部直接 import 物理真身（critical / account /
  phases.post_close）。W1-A/T2 全量切断了历史「经 engine 反查保 patch 命中 + 避循环 import」
  设计——``patch("trading.engine._alert_critical")`` / ``monkeypatch(engine, "_mode")`` 等测试
  因 order_state 不再经 engine 模块属性解析而失效，Task 8-19 迁 patch 至物理真身模块路径
  （monkeypatch critical._alert_critical / critical._mode / account.resolve_account_id /
  post_close._seq_for_real_oid / post_close._order_state_to_db）。order_state 现行 engine 反查归零。
- 项目级单例（``state_store`` / ``clock``）顶部直接 import（不涉及 engine patch，无循环）。

幂等红线（fill 表 UNIQUE(order_id, traded_time) + _fill_inserted 守卫，08-04「1 笔成交记
24 次」事故根因修复）：``handle_order_update`` 的 trade 分支 ``insert_fill`` /
``apply_fill_to_position`` / ``insert_trade_event FILLED`` / 止盈挂单 / 钉钉 fire_and_forget
逐行原样搬，不改任何幂等判定/落账顺序。状态机语义归 #5 权威，不变形。
"""
import logging
from typing import Any, Dict, Mapping, Optional, Callable
from datetime import datetime

from trading import clock, state_store as _state_store
from trading.types.order_state import OrderState
# _CriticalHalt 异常类（L1 致命停调度信号）：critical 定义、engine re-export 同一对象。
# 不被 patch（异常类型识别），顶部直接 import 安全（无循环：critical 不反向 import 本文件）。
# W1-A/T2-Task4：_mode / _alert_critical 反查切断 → 同 critical 顶部直接 import
# （patch engine._mode / engine._alert_critical 失效 → Task 8-19 迁 monkeypatch critical._mode 等）。
from trading.critical import _CriticalHalt, _mode, _alert_critical
# W1-A/T2-Task5：_resolve_account_id 反查切断 → 顶部直接 import trading.account
# SSoT 真身（account 是叶子模块无环 · patch engine._resolve_account_id 失效 → Task 8-19 迁）。
from trading.account import resolve_account_id as _resolve_account_id
# W1-A/T2-Task6：place_take_profit / _seq_for_real_oid / _order_state_to_db 反查切断
# → 直接 import phases.exit / phases.post_close 真身（非历史 engine 模块反查）。
# W1-A/T2-Task10 修：改 **函数内 lazy import**（非顶部）。Why：顶部 import 触发跨包环——
# broker.base → trading.__init__ → order_state → phases.exit → gateway_service → broker.base
# （broker.base 部分初始化, OrderResult 未定义 → ImportError）。Task 6 Step 3 只验了
# `import trading.phases.X`（trading 先加载不触发环），未验 `import broker.qmt`（broker 先
# 加载 → 环）。影响 8 个测试文件（含 test_qmt_health_guard._gw() / Task 12）。
# plan line 116 明示「若仍环暂保 lazy」——按预授权改 lazy。
# patch 语义：函数内 `from trading.phases.X import Y` 在 call-time 绑定 local 名 →
# patch("trading.phases.X.Y") 先于调用 → local 拿 mock（与顶部 import 的 patch 命中等价，
# 因 patch 改的是模块属性 · call-time from-import 读模块属性拿 mock）。已验无 test patch
# 直打 order_state.place_take_profit/_seq_for_real_oid/_order_state_to_db（grep 确认）→
# 改 lazy 对 Task 8-19 patch 迁移语义零影响。

# 日志 logger 名硬编码 ``trading.engine``（而非 ``__name__``=trading.order_state）：
# 3 个 broker 回调函数原是 TradingEngine 实例方法，日志打到 trading.engine logger。
# 迁 order_state.py 后保 logger 名不变 = 保「逐行原样搬」的观测面等价（运维按
# trading.engine 过滤/聚合成交回报日志不断 + test_order_event_rejected_logs_status_msg
# 等 caplog 断言命中）。与 critical.py 用 trading.critical logger 不同：critical 是独立
# 基础设施域（L1 停调度），order_state 的 broker 回调本质是 engine 运行时日志。
logger = logging.getLogger("trading.engine")


class OrderStateMachine:
    """
    订单状态机

    支持的状态迁移：
    1. 正常流程：PENDING -> SUBMITTED -> FILLED
    2. 部分成交：PENDING -> SUBMITTED -> PARTIAL_FILLED -> FILLED
    3. 取消：PENDING -> SUBMITTED -> CANCELLED
    4. 拒绝：PENDING -> SUBMITTED -> REJECTED
    5. 部分取消：PENDING -> SUBMITTED -> PARTIAL_FILLED -> PARTIAL_CANCELLED -> FILLED
    6. 异常处理：任何【非终态】 -> FAILED（终态封闭，不可逆；submit 前含 PENDING）
    """

    def __init__(self):
        """初始化状态机"""
        self.current_state = OrderState.PENDING
        self.order_id: Optional[str] = None
        self.order_info: Optional[Dict[str, Any]] = None
        self.callbacks: Dict[OrderState, Optional[Callable]] = {
            state: None for state in OrderState
        }

    def submit(self, order_info: Dict[str, Any]) -> bool:
        """
        提交订单

        参数：
            order_info: 订单信息字典

        返回：
            是否成功提交
        """
        if self.current_state != OrderState.PENDING:
            raise ValueError(f"当前状态 {self.current_state} 不支持提交订单")

        self.order_info = order_info
        # C-6 V3：order_id 唯一性时间戳走 clock.now（单一口子，测试 monkeypatch 冻结）。
        self.order_id = f"ORDER_{clock.now().strftime('%Y%m%d%H%M%S%f')}"

        # 状态迁移：PENDING -> SUBMITTED
        self._transition_to(OrderState.SUBMITTED)

        return True

    def fill(self, filled_shares: int, filled_price: float) -> bool:
        """
        成交（完全成交或部分成交）

        参数：
            filled_shares: 成交股数
            filled_price: 成交价格

        返回：
            是否成功更新状态
        """
        if self.current_state not in [OrderState.SUBMITTED, OrderState.PARTIAL_FILLED]:
            raise ValueError(f"当前状态 {self.current_state} 不支持成交")

        # 更新成交信息
        if "filled_shares" not in self.order_info:
            self.order_info["filled_shares"] = 0
        if "filled_price" not in self.order_info:
            self.order_info["filled_price"] = []

        self.order_info["filled_shares"] += filled_shares
        self.order_info["filled_price"].append(filled_price)

        # 判断是否完全成交
        if self.order_info["filled_shares"] >= self.order_info["shares"]:
            # 完全成交
            self._transition_to(OrderState.FILLED)
        else:
            # 部分成交
            self._transition_to(OrderState.PARTIAL_FILLED)

        return True

    def cancel(self) -> bool:
        """
        取消订单

        返回：
            是否成功取消
        """
        if self.current_state not in [OrderState.SUBMITTED, OrderState.PARTIAL_FILLED]:
            raise ValueError(f"当前状态 {self.current_state} 不支持取消")

        # 判断是否有部分成交
        if self.current_state == OrderState.PARTIAL_FILLED:
            # 部分取消
            self._transition_to(OrderState.PARTIAL_CANCELLED)
        else:
            # 完全取消
            self._transition_to(OrderState.CANCELLED)

        return True

    def reject(self, reason: str) -> bool:
        """
        拒绝订单

        参数：
            reason: 拒绝原因

        返回：
            是否成功拒绝
        """
        if self.current_state != OrderState.SUBMITTED:
            raise ValueError(f"当前状态 {self.current_state} 不支持拒绝")

        self.order_info["reject_reason"] = reason
        self._transition_to(OrderState.REJECTED)

        return True

    def fail(self, reason: str) -> bool:
        """
        失败（异常处理）：支持从【任意非终态】迁移到 FAILED（含 PENDING）。

        参数：
            reason: 失败原因

        返回：
            是否成功标记为失败

        边界（应修项2）：
            - order_info 可能为 None（submit 前调用，如构造期/网络异常兜底），
              此处惰性初始化为 {}，防 TypeError；
            - 终态（FILLED/CANCELLED/REJECTED）不可再迁移到 FAILED（终态封闭，
              已成交单标失败会让风控/对账误判），由 _is_valid_transition 拒绝。
        """
        # order_info 为 None 时惰性初始化（submit 前调用场景），防 NoneType 不可下标。
        if self.order_info is None:
            self.order_info = {}
        self.order_info["fail_reason"] = reason
        self._transition_to(OrderState.FAILED)

        return True

    def register_callback(self, state: OrderState, callback: Callable):
        """
        注册状态回调

        参数：
            state: 状态
            callback: 回调函数
        """
        self.callbacks[state] = callback

    def _transition_to(self, new_state: OrderState):
        """
        状态迁移（内部方法）

        参数：
            new_state: 新状态
        """
        # 验证状态迁移是否合法
        if not self._is_valid_transition(self.current_state, new_state):
            raise ValueError(f"非法状态迁移: {self.current_state} -> {new_state}")

        # 记录状态迁移
        if "state_history" not in self.order_info:
            self.order_info["state_history"] = []

        self.order_info["state_history"].append({
            "from": self.current_state,
            "to": new_state,
            # C-6 V3：状态迁移事件时间戳走 clock.now（单一口子）。
            "time": clock.now(),
        })

        # 更新状态
        self.current_state = new_state

        # 触发回调
        if self.callbacks[new_state] is not None:
            self.callbacks[new_state](self.order_info)

    def _is_valid_transition(self, from_state: OrderState, to_state: OrderState) -> bool:
        """
        验证状态迁移是否合法

        参数：
            from_state: 起始状态
            to_state: 目标状态

        返回：
            是否合法
        """
        # 定义合法的状态迁移
        valid_transitions = {
            # PENDING 允许 FAILED：submit 前异常兜底（网络/构造期失败），见 fail()。
            OrderState.PENDING: [OrderState.SUBMITTED, OrderState.FAILED],
            OrderState.SUBMITTED: [OrderState.PARTIAL_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED],
            OrderState.PARTIAL_FILLED: [OrderState.PARTIAL_FILLED, OrderState.FILLED, OrderState.PARTIAL_CANCELLED, OrderState.FAILED],
            OrderState.PARTIAL_CANCELLED: [OrderState.FILLED],
            OrderState.FILLED: [],  # 终态
            OrderState.CANCELLED: [],  # 终态
            OrderState.REJECTED: [],  # 终态
            OrderState.FAILED: [],  # 终态
        }

        return to_state in valid_transitions.get(from_state, [])

    def get_state(self) -> OrderState:
        """
        获取当前状态

        返回：
            当前状态
        """
        return self.current_state

    def get_order_info(self) -> Optional[Dict[str, Any]]:
        """
        获取订单信息

        返回：
            订单信息字典
        """
        return self.order_info

    def reset(self):
        """
        重置状态机
        """
        self.current_state = OrderState.PENDING
        self.order_id = None
        self.order_info = None


# ============================================================================
# broker 订单回调三分支 + 状态推进（T1 Task 3 · 集群 I · 缝合点 #2）
# 原 TradingEngine 实例方法 _handle_order_update / _order_direction /
# _advance_order_state_from_status 迁此为 free function（接收 engine 引用）。
# 逐行原样搬移（幂等红线零容忍，状态机语义归 architecture/05 红线不变形）。
# ============================================================================

async def handle_order_update(engine, update: Mapping[str, Any]) -> None:
    """成交回报 handler（由 Task 11 的 ``_on_order_update`` 经 ``create_task`` 调度，
    主线程事件循环执行）。

    物理意图（spec §6.2 C1，三连）：
        on_stock_trade 回调推送 ``kind=="trade"`` 的成交回报（含真实成交价/量/时间，
        非下单时的预估价），本 handler 顺序执行三件事：
          a. ``record_live_trade`` 补写成交回报日志（CSV，Layer 6 LLM 复盘数据源）；
          b. ``notify_trade_event`` 推钉钉成交通知（fire_and_forget 异步不阻塞回调链）；
          c. 买单成交 + 未挂止盈 → ``_place_take_profit`` 挂限价止盈卖单
             （Phase1 简化版：单一固定止盈价、全额；Phase2 升级为分级状态机复刻
             simulate_exit 的 tp1 部分量 + tp2 剩余量）。

    幂等红线（state_store.has_order(TP1)）：
        on_stock_trade 在部分成交 / 柜台重推时会多次推送同一 order_id 的 trade 回报。
        若每次都重挂止盈卖单 → 同笔持仓挂 N 张卖单 → 超卖敞口致命。故查 DB order 表
        has_order(TP1)，已挂即跳过（跨重启持久，state-store-redesign T12 替代原 _tp_placed 内存）。

    线程安全：
        本方法 async，由主线程 ``create_task`` 调度（Task 11 用
        ``call_soon_threadsafe`` 把网关回调线程的 update 投递回主事件循环）。
        钉钉通知走 ``fire_and_forget``（独立 daemon 线程跑 asyncio.run），不阻塞
        回调链——网关回调线程若被 IM 网络延迟阻塞，会反压柜台行情推送。

    边界与降级（Grill Me）：
        - ``kind != "trade"`` 直接 return（order/order_error 由风控层负责，本 handler
          只处理真实成交）；
        - symbol 缺失或 traded_volume<=0 直接 return（脏数据/撤单回报不应触达写日志
          和挂止盈，否则会把废回报当真实成交落账）；
        - 三连各自 try-except 兜底：任一环节失败（日志写盘失败/钉钉网络故障/止盈挂单
          被风控挡板拒）只记日志，不阻塞后续环节（日志失败仍要通知，通知失败仍要挂止盈）；
        - ``_order_direction`` 返 None（查不到订单方向）时保守按 ``"TRADE"`` 落日志、
          不挂止盈（不误判买卖方向 → 不误挂止盈）。

    T1 Task 3 缝合点 #2：本函数原 ``TradingEngine._handle_order_update`` 实例方法，
    迁 order_state.py 后改为接收 engine 引用的 free function。``self._gw``
    访问点改 ``engine._gw``（网关实例引用）；止盈挂单 T1-Task9 已收口——
    ``place_take_profit`` 顶部直接 import phases.exit 真身（W1-A/T2-Task6 切断历史 engine
    re-export 反查），不再经 engine 实例引用（消除缝合点 #2 的 take_profit 耦合）；
    engine 模块级符号 ``_mode`` / ``_alert_critical`` / ``_resolve_account_id`` 顶部直接
    import 物理真身（W1-A/T2-Task4/5 切断历史 engine re-export 反查）。逐行原样，幂等红线零容忍。
    """
    # T1 Task 3 → W1-A/T2 收口：engine 模块级符号经 engine 反查的设计已全量退役（_mode /
    # _alert_critical / _resolve_account_id / place_take_profit / _seq_for_real_oid /
    # _order_state_to_db 全切顶部直接 import 物理真身 · patch engine._xxx 失效 → Task 8-19 迁
    # patch 物理路径）。_state_store / clock 顶部 import（不涉及 engine patch），
    # _CriticalHalt 同上（异常类）。

    kind = update.get("kind")
    if kind == "async_response":
        # #5 修复：seq→real 映射回填 DB order.broker_oid（撤单/对账唯一可靠锚点）。
        # 原实现 kind!='trade' 直接 return 丢弃本事件 → broker_oid 恒 str(seq) →
        # cancel_order_by_broker_oid_db 永匹配不到行（幽灵单）+ post_close TP_FILLED 恒空。
        seq_str = str(update.get("seq", ""))
        real = str(update.get("order_id", ""))
        if seq_str and real and real != seq_str:
            try:
                n = _state_store.update_order_state_by_broker_oid(
                    seq_str, new_broker_oid=real)
                if n == 0:
                    logger.warning(
                        "async_response 未命中 DB 行 seq=%s real=%s（可能 pre_open 未落库）",
                        seq_str, real)
            except Exception:
                # 回填失败 = 撤单/对账锚点失效（可补偿：下次 order/trade 事件按 seq 反查）
                logger.exception(
                    "async_response 回填 broker_oid 失败 seq=%s real=%s（CRITICAL：撤单锚点失效）",
                    seq_str, real)
        return
    if kind == "order":
        # #5 第二刀：柜台委托状态推送（含累计 traded_volume）→ 推进 DB order state。
        # 中间态（SUBMITTED）更新为同值 no-op；终态/部分态精确落库。
        advance_order_state_from_status(engine, update)
        return
    if kind != "trade":
        return  # 仅处理成交回报（order/order_error 由风控层负责，不在本 handler 范围）
    symbol = update.get("stock_code", "")
    qty = update.get("traded_volume", 0)
    price = update.get("traded_price", 0.0)
    order_id = str(update.get("order_id", ""))
    if not symbol or qty <= 0:
        # 脏数据/撤单回报（traded_volume=0）不应落账或挂止盈，直接跳过
        return

    # 判定方向（BUY/SELL/None）——账本写入与挂止盈决策都依赖
    direction = order_direction(engine, order_id)
    if direction is None:
        # #1：方向未知 = 审计黑洞（不挂止盈 + 不落账），必须叫醒人工对账，禁止静默。
        # Fix1（用户两轴 review · 告警模式闸）：dry_run 模式理论上无真实成交回报
        # （无真单），方向未知只会在脏 mock/测试数据中出现，推钉钉纯噪音。
        # live 模式才是真审计黑洞需叫醒人工。守卫与既有 _alert_critical 范式一致。
        if _mode() == "live":
            _alert_critical(
                f"成交回报方向未知 order_id={order_id} symbol={symbol} qty={qty} "
                f"（DB 无 side、内存无 order_type，需人工对账补账）")

    # C-6 V2：TP1 幂等 key（trade_date 口径）与账本 account/trade_id 同源计算一次。
    today_tp = clock.today()
    _account_id = _resolve_account_id()
    _trade_id = _state_store.build_trade_id(_account_id, symbol, today_tp)

    # spec §A1：direction=None 旁路补 trade_event(DIRECTION_UNKNOWN) 审计（Fix 3b，
    # 用户 final review 抓出）。原旁路只有 _alert_critical（仅 live 模式推钉钉），
    # dry_run 模式下方向未知回报在事件流里完全无痕迹 → 事后复盘无法对账（审计黑洞）。
    # trade_event 审计不受 _mode 守卫限制（任何模式都该留痕），与 _alert_critical
    # （live 才推钉钉的告警通道）解耦。UNIQUE(account_id, trade_id, action) 天然幂等，
    # 同 (order_id, traded_time) 重放不重复落行（与 fill 表去重同口径）。
    if direction is None:
        try:
            # 确保 account 行存在（trade_event FK 引用 account，与下方 BUY/SELL
            # 分支同范式——dry_run 影子期可能未预置 default 账户，缺失则 FK 失败）
            if _state_store.get_account(_account_id) is None:
                _state_store.upsert_account(_account_id, broker="qmt")
            _state_store.insert_trade_event(
                _account_id, _trade_id, symbol, "DIRECTION_UNKNOWN",
                order_id=order_id, qty=float(qty) if qty else None,
                price=float(price) if price else None,
                meta=f"reason=direction_unknown|update={update.get('traded_time')}")
        except Exception:
            # 审计旁路软降级：不阻断 handler 主路径（与 _alert_critical 同范式，
            # DB 写失败不抛——审计缺失由日志告警供人工补对账）
            logger.exception(
                "direction=None trade_event 审计失败 symbol=%s order_id=%s",
                symbol, order_id)

    # ── d. 成交账本写入（真相源，最先做——先落账再挂止盈/落日志，防 crash 窗口账账不符）──
    # state-store-redesign §4.2 + W3.1（gateway-ssot-hardening）：
    #   state_store.insert_fill 是成交回报的**唯一幂等真相源**（UNIQUE(order_id, traded_time)）。
    #   CSV 审计镜像（record_live_trade kind=fill）+ 钉钉通知（notify_trade_event）+
    #   position 累加（apply_fill_to_position）+ FILLED 事件（insert_trade_event）
    #   必须与 insert_fill **同一判定点**——首次写入（inserted=True）才执行，重放（False）
    #   全部跳过。这是 08-04 事故（同笔成交重放 24 次致简报「买 24 笔」+ 钉钉轰炸）的根因修复：
    #   原实现把 CSV/钉钉放在 insert_fill 幂等判定**之外**无条件调，与真相源不同判定点 → 镜像失真。
    _fill_inserted = False  # 是否首次成功落 fill（重放=False 时跳过所有镜像写入）
    if direction in ("BUY", "SELL"):
        try:
            # 确保 account 行存在（fill/trade_event FK 引用 account）
            if _state_store.get_account(_account_id) is None:
                _state_store.upsert_account(_account_id, broker="qmt")
            traded_time = str(update.get("traded_time", ""))
            _fill_inserted = _state_store.insert_fill(
                order_id, _account_id, traded_time, symbol, direction,
                float(qty), float(price), strategy="neckline")
            if _fill_inserted:
                # insert_fill 首次入账才更新 position（避免重推重复累加）
                _state_store.apply_fill_to_position(
                    _account_id, symbol, direction, float(qty), float(price), traded_time)
                # FILLED 事件（W3.1：与真相源同判定点——首次 fill 才记 FILLED，
                # 重放不再追加事件行，保证事件流与 fill 表 1:1 对齐）
                _state_store.insert_trade_event(
                    _account_id, _trade_id, symbol, "FILLED",
                    order_id=order_id, qty=float(qty), price=float(price))
                # SSoT Phase B · B2b：BUY 成交写持仓归因（接线 engine 成交路径）。
                # 物理意图：原 record_position_attribution 全仓无生产调用方，归因散在
                # gateway_service 内存字典重启即丢。B2 在 apply_fill_to_position 后接线，
                # 把 strategy/entry_rationale 落 position 表（与 qty/avg_price 同行）。
                # 重启后归因随持仓行存活——「重启后归因不丢」验收数据来源。
                # SELL 不调 clear：apply_fill_to_position 归零删 position 行（state_store.py
                # DELETE WHERE qty=0），归因随行消失——clear 会 UPDATE 0 行（空操作）。
                # 断点-3 Resolution：position 行删除即归因消失（非 clear 调用）。
                # 风控红线：try/except 不阻断成交主路径（成交是交易红线，归因是审计，
                # 失败可补偿——与上方 fill/position 异常升 L1 不同，归因异常软降级）。
                if direction == "BUY":
                    try:
                        from trading.gateway_service import \
                            record_position_attribution
                        record_position_attribution(
                            symbol, "neckline", f"成交建仓@{traded_time}")
                    except Exception:
                        logger.exception(
                            "归因登记失败 symbol=%s traded_time=%s（不阻断成交主路径）",
                            symbol, traded_time)
            else:
                # 重放（insert_fill 命中 UNIQUE 返 False）：CSV/钉钉/position 全部跳过。
                # 物理意图：on_stock_trade 在部分成交/柜台重推时会重放同一 (order_id, traded_time)，
                # 真相源已挡住重复入库，镜像（CSV/钉钉）必须同判定点同步挡住，否则审计旁路与
                # 真相源漂移（08-04 事故 1 笔成交被记 24 次）。
                logger.info(
                    "成交回报重复，跳过 CSV/钉钉/position（order_id=%s traded_time=%s）",
                    order_id, traded_time)
        except Exception as e:
            # #5/A5：C-4 分级——敞口真相失真 = L1 停调度（宁可停不可带病跑）。
            # 原软降级会让 fill/position 静默缺失，对账只能事后发现。
            logger.exception("成交回报落账失败 symbol=%s order_id=%s", symbol, order_id)
            raise _CriticalHalt(
                f"成交回报落账失败 symbol={symbol} order_id={order_id}"
                f"（fill/position 真相源失真）") from e

    # ── c. 买单成交 + 未挂止盈 → 挂限价止盈卖单（DB 幂等防重挂）──
    # 卖单成交（direction=="SELL"）无需挂止盈（卖出即离场，无持仓可止盈）。
    # 方向未知（None）保守不挂——宁可漏挂止盈让人工补，也不误把卖单当买单挂反方向单。
    # 注：TP 挂单的幂等独立于 fill（has_order(TP1) DB 查询），与 _fill_inserted 不耦合
    # （fill 重放时 TP 可能因 has_order 已 True 而跳过，但两套幂等各管各的真相源）。
    _tp_already = False
    try:
        _tp_already = _state_store.has_order(_account_id, today_tp, symbol, "TP1")
    except Exception:
        logger.exception("查 DB has_order(TP1) 失败 symbol=%s（保守跳过，防重复挂）", symbol)
        _tp_already = True  # DB 查询失败保守视为已挂（宁可漏挂人工补，不超卖）
    if direction == "BUY" and not _tp_already:
        # W1-A/T2-Task10：函数内 lazy import 防跨包环（broker.base→trading→order_state→
        # phases.exit→gateway_service→broker.base）；plan line 116「若仍环暂保 lazy」预授权。
        from trading.phases.exit import place_take_profit
        try:
            await place_take_profit(symbol, qty, price, order_id)
        except Exception:
            # 止盈挂单失败（被风控挡板拒/网关断线）不抛——人工补挂（告警已记日志）。
            logger.exception("挂止盈失败 symbol=%s（需人工补挂）", symbol)

    # ── a/b. 成交日志（CSV）+ 钉钉通知（W3.1：与 fill 真相源同判定点）──
    # 方向已知（BUY/SELL）：仅在 _fill_inserted=True（首次落账）时写 CSV + 推钉钉。
    #   重放（_fill_inserted=False）→ 完全跳过，保证 CSV/钉钉与 fill 表 1:1（08-04 事故根因）。
    # 方向未知（None）：W3 完整收口（用户两轴 review，spec §3.3.1「同一判定点」）——
    #   **不再写 CSV / 不再推钉钉**。Why：原 direction=None 旁路无条件写 CSV/推钉钉
    #   （"TRADE" 中性标签），与 insert_fill 不同判定点 —— 同一条「方向未知回报」被重放
    #   N 次会重复落 CSV/推钉钉，污染审计镜像与 IM 通知（重放不幂等）。W3 完整收口选 C
    #   （最干净 + 符合 spec）：direction=None 时 CSV/钉钉也不写，与 fill 表「direction
    #   不在 (BUY,SELL) 时 insert_fill 不被调（无 fill 表行）」同判定点（都不写）；
    #   告警由上方 _alert_critical 承担（人工对账线索），CSV 旁证在重放时反而污染真相
    #   源判定。direction is None 时不进入下方 if 块（CSV/钉钉双跳过）。
    if _fill_inserted:
        # SSoT Phase A · Task A1：原 record_live_trade CSV 审计块已删除（审计平移 trade_event，
        # fill 表本身已是真相源 + 上面 insert_trade_event FILLED 已记事件流，CSV 镜像冗余）。
        # 重放幂等性由 fill 表 UNIQUE(order_id, traded_time) + _fill_inserted 守卫共同保证，
        # 不再依赖 CSV 旁证。NotificationManager 钉钉通知保留（与 fill 表同判定点，首次才推）。
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_trade_event(
                symbol, direction or "TRADE", float(qty), float(price),
            ))
        except Exception:
            logger.exception("成交通知发送失败 symbol=%s", symbol)


def order_direction(engine, order_id: str) -> Optional[str]:
    """从 ``gw._orders`` 查订单方向（BUY/SELL）。

    物理意图：
        成交回报 ``update`` 只含 order_id 与成交价量，**不含下单时声明的买卖方向**。
        必须回查 ``gw._orders[order_id].order_type`` 拿下单时记录的方向枚举
        （下单瞬间由 broker/qmt.py ``_place_order`` 写入 _orders 字典），才能判定
        本次成交是买单（需挂止盈）还是卖单（无需挂止盈）。

    order_type 枚举（xtconstant 契约，与 broker/qmt.py:724 同源）：
        - ``xtconstant.STOCK_BUY = 23``  → 返 "BUY"
        - ``xtconstant.STOCK_SELL = 24`` → 返 "SELL"
        - 其它/缺失 → 返 None（保守，不误挂止盈）

    Args:
        engine: TradingEngine 实例（T1 Task 3 缝合点 #2：原 self，迁此为 engine 引用，
            访问 ``engine._gw`` 拿 gw._orders 字典）。
        order_id: 成交回报里的订单 ID（str；gw._orders 的 key 在 broker/qmt.py 内
                  既可能是 seq 也可能是 real order_id，本处按 str(update["order_id"]) 查）。

    Returns:
        "BUY" / "SELL" / None。None 时调用方（handle_order_update）保守按 "TRADE"
        落日志、跳过挂止盈（不猜方向 → 不误挂反方向单）。

    ⚠️ 测试环境兜底（ImportError）：
        xtconstant 来自 xtquant SDK，CI/单测环境无 xtquant 时 ``from xtquant import
        xtconstant`` 抛 ImportError——此处兜底硬编码 23/24（与 conftest.py 的假
        xtconstant 同值），保证单测可跑。生产环境（miniQMT 通道）xtquant 必装，
        兜底分支不会触达。

    T1 Task 3 缝合点 #2：``self._gw`` → ``engine._gw``；engine 模块级辅助
    ``_seq_for_real_oid`` 顶部直接 import post_close 真身（W1-A/T2-Task6 切断历史 engine
    re-export 反查 · patch engine._seq_for_real_oid 失效 → Task 8-19 迁）。逐行原样。
    """
    # T1 Task 3 → W1-A/T2-Task6 收口：_seq_for_real_oid 已切顶部直接 import post_close 真身
    # （本函数删别名赋值，patch engine._seq_for_real_oid 失效 → Task 8-19 迁 monkeypatch
    # post_close.seq_for_real_oid）。_state_store 顶部 import。

    # #1 修复：方向反查 DB 优先（state_store.order.side，pre_open 已落库），
    # 内存 gw._orders.order_type 仅兜底（_sync_orders_if_stale 走 query_orders 时才有 order_type）。
    # 竞态兜底：DB 按 real 查 miss 时经 _seq_to_real 反查 seq 再查一次（async_response 晚到）。
    _row = None
    # W1-A/T2-Task10：函数内 lazy import 防跨包环（见文件顶部注释 · plan line 116 预授权）
    from trading.phases.post_close import seq_for_real_oid as _seq_for_real_oid
    try:
        _row = _state_store.get_order_by_broker_oid(order_id)
        if _row is None:
            _seq = _seq_for_real_oid(engine._gw, order_id)
            if _seq is not None:
                _row = _state_store.get_order_by_broker_oid(str(_seq))
    except Exception:
        logger.exception("get_order_by_broker_oid 失败 order_id=%s（回退内存）", order_id)
    if _row is not None:
        side = str(_row.get("side") or "").lower()
        if side == "buy":
            return "BUY"
        if side == "sell":
            return "SELL"
        # DB 有行但 side 异常 → 继续走内存兜底，不轻易返 None
    orders = getattr(engine._gw, "_orders", {}) if engine._gw else {}
    rec = orders.get(order_id, {})
    try:
        from xtquant import xtconstant  # 与 broker/qmt.py:61 同源导入路径
        STOCK_BUY = xtconstant.STOCK_BUY
        STOCK_SELL = xtconstant.STOCK_SELL
    except ImportError:
        STOCK_BUY, STOCK_SELL = 23, 24  # CI/单测无 xtquant 兜底（与 conftest 同值）
    ot = rec.get("order_type")
    if ot == STOCK_BUY:
        return "BUY"
    if ot == STOCK_SELL:
        return "SELL"
    return None


def advance_order_state_from_status(engine, update: Mapping[str, Any]) -> None:
    """kind=order：按柜台状态推进 DB order.state/filled_*（#5 第二刀）。

    Why 用 order 事件而非 trade 事件：order_status 55/56 区分 PARTIAL/FILLED，
    traded_volume 是累计成交（trade 是本笔增量），状态推进必须用累计量。
    竞态（async_response 晚到）：按 real 查 miss 时经 _seq_to_real 反查 seq 再匹配。

    T1 Task 3 缝合点 #2：``self._gw`` → ``engine._gw``；engine 模块级辅助
    ``_seq_for_real_oid`` / ``_order_state_to_db`` 顶部直接 import post_close 真身（W1-A/T2-Task6
    切断历史 engine re-export 反查 · 保 ``patch("trading.engine._seq_for_real_oid")`` 历史命中
    已失效 → Task 8-19 迁）。逐行原样。
    """
    # T1 Task 3 → W1-A/T2-Task6 收口：_seq_for_real_oid / _order_state_to_db 已切顶部直接
    # import post_close 真身（本函数删别名赋值，patch engine._seq_for_real_oid /
    # engine._order_state_to_db 失效 → Task 8-19 迁 monkeypatch post_close 物理路径）。

    lookup = str(update.get("order_id", ""))
    if not lookup:
        return
    # W1-A/T2-Task10：函数内 lazy import 防跨包环（见文件顶部注释 · plan line 116 预授权）
    from trading.phases.post_close import (
        seq_for_real_oid as _seq_for_real_oid,
        order_state_to_db as _order_state_to_db,
    )
    row = None
    try:
        row = _state_store.get_order_by_broker_oid(lookup)
        if row is None:
            seq = _seq_for_real_oid(engine._gw, lookup)
            if seq is not None:
                row = _state_store.get_order_by_broker_oid(str(seq))
    except Exception:
        logger.exception("get_order_by_broker_oid 失败 lookup=%s", lookup)
        return
    if row is None:
        logger.warning("order 事件未命中 DB 行 lookup=%s（可能 server 手动单/未落库）", lookup)
        return
    traded_volume = update.get("traded_volume")
    traded_price = update.get("traded_price")
    new_state = _order_state_to_db(update.get("state"))
    old_state = row["state"]
    # 拒因观测补全（08-06 实测：688160.SH 订单被柜台 REJECTED，日志零痕迹——废单
    # 原因黑盒。柜台 status_msg/order_remark 必须随状态变化落日志，否则无法回答
    # 「为什么没成交」）。
    status_msg = str(update.get("status_msg") or update.get("order_remark") or "")
    try:
        n = _state_store.update_order_state_by_broker_oid(
            row["broker_oid"] or lookup,
            state=new_state,
            filled_qty=float(traded_volume) if traded_volume is not None else None,
            filled_price=float(traded_price) if traded_price is not None else None,
        )
        if n == 0:
            logger.warning("order 状态推进未命中 broker_oid=%s（下个事件补推进）", row.get("broker_oid"))
        elif old_state != new_state:
            logger.info(
                "订单状态推进 oid=%s symbol=%s %s → %s status_msg=%r",
                row["broker_oid"] or lookup, row.get("symbol", "?"),
                old_state, new_state, status_msg)
    except Exception:
        logger.exception("order 状态推进失败 lookup=%s（软降级，下个事件补推进）", lookup)