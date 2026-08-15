"""
broker/qmt_io.py
================
QmtExecutionGateway IO 层 mixin（W2-H1 · broker 四文件分层 · 逻辑只搬）。

接缝说明（为什么在这层）：
- 本模块承载网关的【查询/IO 六方法】：持仓对账拉取（_fetch_broker_positions）、
  资金查询（query_asset）、账号主动探针（probe_account_status）、实时行情
  （get_quote）、当日委托/成交查询（query_orders/query_trades）——共性是「向柜台
  发只读查询、清洗返回、统一降级口径」，零订单状态机副作用。
- 类组装于 broker/qmt.py：``QmtExecutionGateway(QmtBusinessMixin, QmtIoMixin,
  QmtConnectionBase)``。共享状态全部走 self 属性（_loop/_trader/_account/_orders/
  is_blocked 等，初始化在 QmtConnectionBase.__init__，锁风控状态机在
  QmtBusinessMixin）——与分层前同一实例世界，mixin 间零新通信机制。
- 共享常量从 broker.qmt_connection（契约根）from-import：不可变值拷贝语义，
  读取方单一（本层仅 _ORDER_TIMEOUT/_map_qmt_status）；单测若需 patch 超时须指
  本模块（T1 范式：patch 须指读取方真身模块，垫片/契约根的副本不生效）。

分层红线（spec §5.1）：逻辑只搬位置 + 接缝注释，零行为改动；方法体逐字保留。
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

from broker.qmt_connection import _ORDER_TIMEOUT, _map_qmt_status
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身

# ⚠️ 日志名锁定 "broker.qmt"（不用默认 __name__）：分层前全部日志经 broker.qmt 一个
# logger 出；分层后三文件共用同名 logger，保 caplog 过滤与运维日志口径零变化。
import logging

logger = logging.getLogger("broker.qmt")


class QmtIoMixin:
    """QmtExecutionGateway 的 IO 六方法（查询/对账/行情，只读无状态机副作用）。"""

    # ---------------------------------------------------------- 持仓对账
    async def _fetch_broker_positions(self, *, tradable_only: bool = True) -> Mapping[str, Mapping[str, Any]]:
        """
        拉取券商真实持仓并清洗为 {stock_code: {volume, avg_price, open_price, yesterday_volume}}
        （模板方法 _fetch_broker_positions 实现，T7 扩展字段）。

        边界与清洗（Grill Me）：
        - query_stock_positions 返回 None：文档明确「查询失败或当日持仓为空」均返回
          None，二者不可区分，这里统一记 warning 并返回空 dict，避免对账层把「查询
          失败」误当「真实空仓」而触发 only_broker 漂移告警。
        - can_use_volume == 0 过滤：T+1 当日买入仓位可用为 0 但确属真实持仓；此处按
          调用方契约过滤「废弃持仓」，意味着本网关对账口径是【可操作持仓】而非【全量
          持仓】。若策略层需要全量敞口对账，应另起查询口径，不可复用本返回值。
        - volume 转 float：QMT 返回 int（股数），对外契约统一 float 以兼容碎股/债券张数。
        - 扩展字段（T7，Why 增量透出）：
          * avg_price 成本价 —— 供浮盈对账（market_value - avg_price*volume），
            原契约只有 volume 只能量敞口不能量盈亏，二期浮盈增强需此字段；
          * open_price 开仓价 —— 与 avg_price 区分（加减仓后 avg 摊薄，open 不变），
            供建仓成本回溯与归因分析；
          * yesterday_volume 昨夜股 —— T+1 判断强化（今日买入不可卖，但昨日持仓可卖），
            与 can_use_volume==0 过滤形成双保险（can_use 是柜台给的可卖数，yesterday
            是端到端语义校验，两者通常一致，分歧时为脏数据早期告警）。

        ⚠️ 破坏性变更：返回结构从 {sym: float} 变为 {sym: dict}。所有读 positions[sym]
        当 float 的消费者必须迁移到 positions[sym]["volume"]。已迁移（T7）：
        - BaseExecutionGateway.sync_positions 内扁平化（保 reconcile 契约不变）；
        - engine.stop_loss_monitor qty 读取；
        - trading_service.get_positions 取 volume 子键。
        """
        if self._loop is None or self._trader is None or self._account is None:
            raise RuntimeError("QMT 网关未连接，无法对账（请先 await connect()）")
        if self.is_blocked:
            raise RuntimeError("QMT 网关已锁定（断线保护），拒绝对账以防脏读")

        positions = await self._loop.run_in_executor(
            None, lambda: self._trader.query_stock_positions(self._account)
        )
        if positions is None:
            logger.warning("query_stock_positions 返回 None（查询失败或当日无持仓）")
            return {}

        cleaned: dict[str, dict[str, Any]] = {}
        for p in positions:
            # 过滤可用为 0 的废弃持仓（已平仓残留 / T+1 冻结不可操作仓）。
            # tradable_only=True（默认）：过滤——供 stop_loss 等只动可卖仓的场景（不能卖 T+1 冻结仓）；
            # tradable_only=False：全量（含 T+1 冻结）——供展示/对账看真实敞口。
            # 修正（2026-07-27）：原展示(get_positions)/对账(sync_positions)复用过滤口径，
            # 致 T+1 真实敞口被藏（研究员看「空仓」实则有茅台 T+1 仓）+ drift 失真。
            if tradable_only and getattr(p, "can_use_volume", 0) == 0:
                continue
            cleaned[p.stock_code] = {
                # volume 主可用量（可卖持仓，消费者扁平化时取此键）
                "volume": float(getattr(p, "volume", 0)),
                # avg_price 成本价（浮盈对账：market_value - avg_price*volume）
                "avg_price": float(getattr(p, "avg_price", 0.0) or 0.0),
                # open_price 开仓价（与 avg_price 区分，建仓成本回溯）
                "open_price": float(getattr(p, "open_price", 0.0) or 0.0),
                # yesterday_volume 昨夜股（T+1 可卖判断双保险，int 股数）
                "yesterday_volume": int(getattr(p, "yesterday_volume", 0) or 0),
            }
        logger.debug("QMT 对账拉取完成：有效持仓 %d 只", len(cleaned))
        return cleaned

    # ---------------------------------------------------------- 资产查询
    async def query_asset(self) -> dict[str, Any]:
        """
        查询资金资产，返标准化 dict（投线程池调 query_stock_asset）。

        返回结构（4 字段，与一期 trading_service.get_asset 的 QMT 内联分支 +
        前端 Asset 类型完全对齐）::

            {"account_id": str, "cash": float, "total_asset": float, "market_value": float}

        Why 4 字段对齐：一期/前端已建立的资产契约就是这 4 字段；frozen_cash 虽然
        XtAsset 里有，但前端不展示、调用方不消费，按 YAGNI 不透出（不破坏对外契约）。

        Why 异常/None/锁定 → 返 {}：
        - None：xttrader.md 明确 query_stock_asset 查询失败/无资产均返 None，二者
          不可区分，统一返 {} 让调用方按「资产缺失」降级（与一期 get_asset 一致）；
        - 异常/超时：柜台无响应或网络抖动时 wait_for 抛 TimeoutError，返 {} 让
          二期 circuit_breaker 跳过当日损失检查（跳过≠熔断，避免误触发强平）；
        - 锁定：断线/账号 DISABLEBYSYS 窗口期内 query_stock_asset 可能返回陈旧
          快照，若透出会让熔断基于错乱 equity 误判，故与 submit_order 同口径直接返 {}。

        Why 复用 run_in_executor + wait_for：query_stock_asset 是同步阻塞的 C++
        调用（与 query_stock_positions 同型），直调会卡死事件循环；用既有模式
        投线程池 + _ORDER_TIMEOUT 超时兜底，零新依赖（Karpathy 极简）。

        Why _lock_down 判定在前、连接判定在后：锁定态下即使 _connected=True 也
        必须返 {}（陈旧快照风险）；连接缺失（_trader/_account/_loop 任一为 None）
        本身也不会触发锁定，这里用先锁后连的顺序保持与 submit_order 同语义。

        双消费者（增量不重构）：
        - 一期 trading_service.get_asset 的 QMT 内联分支【保持不动】（它已直接
          内联调 query_stock_asset，重构超出本 task scope）；
        - 本方法主要供二期 circuit_breaker.check_daily_loss_limit 消费
          result["total_asset"] 作为 equity，解锁「二期 live 必修 gap①」
          post_close 熔断连线（此前 circuit_breaker 卡在「无 equity 源」）。
        未来可统一双网关口径（follow-up，非本 task scope）。
        """
        # 连接前置：loop/trader/account 任一缺失即视为未连接，返 {} 防空指针
        if self._loop is None or self._trader is None or self._account is None:
            return {}
        # 锁定（断线/账号 fatal）→ 返 {} 防脏读（与 submit_order 同口径熔断）
        if self.is_blocked:
            logger.warning("QMT 网关已锁定，query_asset 返空（断线保护，防脏读）")
            return {}
        try:
            # 投线程池 + wait_for 超时兜底（与 _fetch_broker_positions / submit_order 同模式）
            asset = await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, lambda: self._trader.query_stock_asset(self._account)),
                timeout=_ORDER_TIMEOUT,
            )
        except Exception as exc:
            # 超时/异常不抛——让 circuit_breaker 跳过当日损失检查（跳过≠熔断）
            logger.exception("QMT query_stock_asset 异常/超时(>%ss)：%s", _ORDER_TIMEOUT, exc)
            return {}
        if asset is None:
            # xttrader.md：查询失败/无资产均返 None，统一返 {} 让调用方按缺失降级
            return {}
        # float(x or 0.0) 双保险：防 None（缺字段）/ NaN（脏数据）导致下游聚合异常
        return {
            "account_id": str(getattr(asset, "account_id", "") or ""),
            "cash": float(getattr(asset, "cash", 0.0) or 0.0),
            "total_asset": float(getattr(asset, "total_asset", 0.0) or 0.0),
            "market_value": float(getattr(asset, "market_value", 0.0) or 0.0),
        }

    # ---------------------------------------------------------- T9 主动探针
    async def probe_account_status(self, *, timeout: float = 5.0) -> tuple[bool, str]:
        """T9 主动探针：query_account_status 判客户端存活性（补 on_disconnected 僵死盲区）。

        物理意图：_health_guard 第②步 _connected=True 时调——socket 看似连着但客户端
        可能僵死（重启中/假死，on_disconnected 不触发）。query_account_status 无参同步 API
        （xtquant/xttrader.py:668），客户端僵死时超时/抛异常 → 探针失败。_health_guard 计
        连续 N 次失败 → 判僵死，置 _connected=False 走重连。

        与 query_asset 的区别：query_asset 失败/正常/锁定均返 {}（不可作僵死判据）；本探针
        明确区分成功（API 有响应=客户端活着）vs 失败（异常/超时/None）。

        防御性口径（option 2 · N 待模拟盘 CSV 实证微调）：query_account_status 返任何非 None
        值（含负数 rc，柜台业务错误但客户端仍应答了）= 客户端活着（ok=True）；异常/超时/None
        = 探针失败（ok=False）。此口径下探针是「严格改进」——比现状（② no-op 完全盲）多抓
        异常/超时类僵死；若客户端返缓存值假活（false-negative），与现状同（不更糟），CSV 验证。

        Returns:
            (ok, detail)：ok=True 客户端响应了（活着）；ok=False 探针失败（detail 含原因）。
        """
        if self._loop is None or self._trader is None:
            return False, "loop/trader 未装配"
        try:
            rc = await asyncio.wait_for(
                self._loop.run_in_executor(None, self._trader.query_account_status),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return False, f"探针超时（{timeout}s，疑客户端僵死）"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        # 任何非 None 返回 = 客户端应答了（活着）；None = 查询无应答（疑僵死）
        if rc is None:
            return False, "query_account_status 返 None（无应答）"
        return True, f"rc={rc}"

    # ---------------------------------------------------------- 实时行情
    async def get_quote(self, symbol: str) -> Optional[Mapping[str, Any]]:
        """单标的实时快照（BaseExecutionGateway.get_quote 实现，spec §3.3 新增契约）。

        委托同包 ``broker.qmt_quote.get_quote``（原 trading.qmt_market_data 模块级
        自由函数的真身，Layer2 阶段3 git mv 迁入）。Why 委托而非内联：行情清洗
        （xtdata 驼峰归一化 + 涨跌停注入 + 按日缓存）是独立复杂度，已封装在
        qmt_quote，这里只做一层 method 转发，让 QmtExecutionGateway 满足基类
        抽象契约的同时复用既有清洗逻辑（DRY）。

        降级语义同 qmt_quote.get_quote：xtdata 不可用 / 异常 / 标的不存在 → 返 None，
        调用方（risk_shield 涨跌停关 / stop_loss 现价检查）按 None 降级跳过。
        """
        from broker.qmt_quote import get_quote as _get_quote
        return await _get_quote(symbol)

    # ---------------------------------------------------------- 委托/成交查询
    async def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        """查询当日委托（投线程池调 query_stock_orders），返标准化 dict 列表。

        用途（Why 主动查询，非主推替代）：
        - subscribe 失败兜底（T5 惰性同步 _orders）：connect 时 sub_rc!=0 主推缺失，
          本方法是订单状态盲区的唯一回填路径；
        - 二期盘后对账强化：不止持仓对账，还能对委托流水做完整性核对（缺单/漏单
          与本地 _orders 的差分）。

        字段映射（xttrader.md XtOrder）：
        - order_id/stock_code/order_type/order_volume/price/traded_volume/
          traded_price/order_status/status_msg/order_remark 原样透出；
        - 额外补 state 字段：_map_qmt_status(order_status) 返 **OrderState 枚举**
          （非字符串），与 on_stock_order 回调存 _orders 的 state 同型，亦与
          circuit_breaker._TERMINAL（frozenset[OrderState]）同型。T5 惰性同步
          merge _orders 时直接可用无需类型转换；保持 _orders 枚举一致性，避免
          circuit_breaker 终态判定踩「枚举≠字符串、OrderState.FILLED not in
          {...字符串...} 恒 True → 已成交单被误判非终态」陷阱。对外 JSON 序列化
          （如未来 GET /orders）留 API 层处理（一期 get_orders 已 dict 化 _orders
          有先例）。

        降级语义（Why None/异常/锁定 → 返 []，对齐 query_asset 的 {} 降级口径）：
        - None：query_stock_orders 查询失败/当日无委托均返 None（不可区分），返 []
          让调用方按空降级；
        - 异常/超时：柜台无响应时 wait_for 抛 TimeoutError，返 [] 不让上层崩；
        - 锁定：断线/账号 DISABLEBYSYS 窗口期可能返陈旧快照，与 submit_order
          同口径直接返 [] 防脏读。

        Why 复用 run_in_executor + wait_for：query_stock_orders 是同步阻塞的 C++
        调用（与 query_stock_asset 同型），直调会卡死事件循环；用既有模式投
        线程池 + _ORDER_TIMEOUT 超时兜底，零新依赖（Karpathy 极简）。

        Args:
            cancelable_only: True=只返可撤单（未到终态）；False=全量。透传给
                query_stock_orders 的同名参数。
        """
        # 连接前置：loop/trader/account 任一缺失即视为未连接，返 [] 防空指针
        if self._loop is None or self._trader is None or self._account is None:
            return []
        # 锁定（断线/账号 fatal）→ 返 [] 防脏读（与 query_asset / submit_order 同口径）
        if self.is_blocked:
            return []
        try:
            # lambda 闭包捕获 cancelable_only，投线程池同步执行后 await 拿结果
            orders = await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, lambda: self._trader.query_stock_orders(
                        self._account, cancelable_only)),
                timeout=_ORDER_TIMEOUT,
            )
        except Exception as exc:
            # 超时/异常不抛——让上层（T5/对账）按 [] 降级
            logger.exception("QMT query_stock_orders 异常/超时(>%ss)：%s", _ORDER_TIMEOUT, exc)
            return []
        if not orders:
            # None 或空列表统一返 []（None 即查询失败/当日无委托，二者不可区分）
            return []
        # getattr(o, "xxx", 默认) 防缺字段；float 字段 `or 0.0` 防 None/NaN
        return [{
            "order_id": getattr(o, "order_id", 0),
            "stock_code": getattr(o, "stock_code", ""),
            "order_type": getattr(o, "order_type", 0),
            "order_volume": getattr(o, "order_volume", 0),
            "price": float(getattr(o, "price", 0.0) or 0.0),
            "traded_volume": getattr(o, "traded_volume", 0),
            "traded_price": float(getattr(o, "traded_price", 0.0) or 0.0),
            "order_status": getattr(o, "order_status", 255),
            # state 与 on_stock_order 回调同源映射（56→FILLED 等），保证状态语义一致
            # Why 返 OrderState 枚举（非 .name 字符串）：query_orders 当前唯一消费者
            # 是 T5 惰性同步 merge _orders（内部枚举世界）；返枚举与 _orders 内部
            # state 类型对齐，T5 直接 merge 安全无类型转换；circuit_breaker._TERMINAL
            # 是 frozenset[OrderState]（枚举集），若 state 为字符串会触发
            # OrderState.FILLED not in {"FILLED",...} 恒 True → 已成交单误判非终态
            # 陷阱。对外 JSON 序列化（如未来 GET /orders）留 API 层处理（一期
            # get_orders 已 dict 化 _orders 有先例）。
            "state": _map_qmt_status(getattr(o, "order_status", 255)),
            "status_msg": getattr(o, "status_msg", ""),
            "order_remark": getattr(o, "order_remark", ""),
        } for o in orders]

    async def query_trades(self) -> list[dict[str, Any]]:
        """查询当日成交（投线程池调 query_stock_trades），返标准化 dict 列表。

        用途与降级语义同 query_orders：subscribe 失败兜底 + 二期盘后成交对账；
        None/异常/锁定 → 返 []（对齐 query_asset/query_orders 的降级口径）。

        字段映射（xttrader.md XtTrade）：order_id/stock_code/traded_volume/
        traded_price/traded_amount/traded_time；traded_price/traded_amount 为 float
        字段，用 `or 0.0` 防 None/NaN 脏数据导致对账聚合异常。
        """
        if self._loop is None or self._trader is None or self._account is None:
            return []
        if self.is_blocked:
            return []
        try:
            trades = await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, lambda: self._trader.query_stock_trades(self._account)),
                timeout=_ORDER_TIMEOUT,
            )
        except Exception as exc:
            logger.exception("QMT query_stock_trades 异常/超时(>%ss)：%s", _ORDER_TIMEOUT, exc)
            return []
        if not trades:
            return []
        return [{
            "order_id": getattr(t, "order_id", 0),
            "stock_code": getattr(t, "stock_code", ""),
            "traded_volume": getattr(t, "traded_volume", 0),
            "traded_price": float(getattr(t, "traded_price", 0.0) or 0.0),
            "traded_amount": float(getattr(t, "traded_amount", 0.0) or 0.0),
            "traded_time": getattr(t, "traded_time", 0),
        } for t in trades]
