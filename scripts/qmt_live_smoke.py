# -*- coding: utf-8 -*-
"""miniQMT 全方位实盘联调脚本（模拟仓 · T1-T6 补全验证）。

物理意图：趁开盘时段，对 QmtExecutionGateway 的全部对外接口做真实柜台联调，
验证 T1-T6 补全（on_account_status / query_asset / get_quotes / query_orders /
query_trades / subscribe 兜底）+ 既有接口（connect / positions / submit_order /
cancel_order / 回调推送）在真实 miniQMT 模拟盘下端到端可用。单元测试用 mock，
本脚本用真实 xtquant + 真实柜台，是「mock 过但真柜台没过」的最后一道闸门。

前置条件：
  1. miniQMT 客户端已启动并登录模拟盘账号（东北证券 NET 10110356 或 .env 配置）
  2. userdata_mini 目录已生成（QMT_USERDATA_PATH 指向）
  3. .env 已配 QMT_USERDATA_PATH / QMT_ACCOUNT_ID / QMT_SESSION_ID
  4. .venv310 环境（xtquant 绑 3.10）

运行：.venv310/Scripts/python.exe scripts/qmt_live_smoke.py

铁律（CLAUDE.md 状态机边界 + 模拟仓无顾忌但仍守序）：
  - 每步 input() 等待人工确认，绝不批量自动跑真单（防止意外多发）。
  - 步骤 10 真单需显式 YES，直连网关（不经 risk_shield 挡板），仅限模拟盘小额。
  - 模拟仓无真实资金风险，但逻辑缺陷（如撤单链路）会被本脚本暴露。

覆盖矩阵（T1-T6 + 既有）：
  T1  on_account_status    — 步骤 9（被动观察账号状态推送 / _lock_down）
  T2  query_asset          — 步骤 2（真实资产 4 字段）
  T3  get_quote/get_quotes — 步骤 6/7（单只 + 批量行情）
  T4  query_orders/trades  — 步骤 4/5（当日委托 + 成交）
  T5  _sync_orders_if_stale— 步骤 8（惰性同步，_main_push_available 触发）
  既有 connect/positions   — 步骤 1/3
  既有 submit/cancel       — 步骤 10（真单 + 撤单 + 回调推送观察）
"""
import asyncio
import os
import sys

# 把项目根加入 sys.path（脚本独立运行需要）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# 触发 .env 加载
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from trading.qmt_gateway import QmtExecutionGateway
from trading import qmt_market_data
from trading.execution_gateway import OrderRequest
from trading.order_state import OrderState


# === 回调推送观察（注册到 gw，主线程 create_task 调度）=========================
_push_count = {"order": 0, "trade": 0, "order_error": 0, "cancel_error": 0, "async_response": 0}


async def _on_order_update(update: dict) -> None:
    """订单更新回调（gw.set_order_update_callback 注册）：打印每条推送 + 计数。"""
    kind = update.get("kind", "?")
    _push_count[kind] = _push_count.get(kind, 0) + 1
    sym = update.get("stock_code", "")
    state = update.get("state")
    state_name = state.name if hasattr(state, "name") else state
    print(f"  [推送#{_push_count[kind]}] kind={kind} sym={sym} state={state_name} "
          f"oid={update.get('order_id')} traded={update.get('traded_volume', '')}")


def _step(title: str) -> bool:
    """打印步骤标题，等待回车。返回 True 表示用户要求退出。"""
    print(f"\n{'=' * 64}\n=== {title}\n{'=' * 64}")
    return input("回车继续，q 退出：").strip().lower() == "q"


def _ok(cond: bool, label: str) -> None:
    """打印 ✅/❌ + 标签，便于一眼扫通过情况。"""
    print(f"  [{'✅' if cond else '❌'}] {label}")


async def main():
    print("miniQMT 全方位实盘联调启动（模拟仓 · T1-T6 验证）")
    print(f"account={os.getenv('QMT_ACCOUNT_ID')}  userdata={os.getenv('QMT_USERDATA_PATH')}")
    print(f"session={os.getenv('QMT_SESSION_ID', '123456')}  mode=模拟盘直连网关")
    print("[!] 步骤 10 真单直连网关不经 risk_shield，仅限模拟盘小额验证。")

    gw = QmtExecutionGateway()
    # 注册订单更新回调（观察 on_stock_order/on_stock_trade/on_order_error/on_cancel_error/
    # on_order_stock_async_response 经主线程调度后的统一推送）
    gw.set_order_update_callback(_on_order_update)

    # --- 步骤 1：connect（既有 + subscribe + _main_push_available）---
    if _step("步骤 1: connect（期望 _connected=True, _lock_down=False, _main_push_available=True）"):
        return
    try:
        await gw.connect()
    except (ConnectionError, Exception) as e:
        print(f"[FAIL] 连接失败：{e}")
        print("请确认 miniQMT 客户端已启动登录，QMT_USERDATA_PATH 路径正确。")
        return
    _ok(gw._connected, f"_connected={gw._connected}")
    _ok(not gw.is_locked, f"is_locked={gw.is_locked}")
    _ok(getattr(gw, "_main_push_available", None) is True,
        f"_main_push_available={getattr(gw, '_main_push_available', '<缺属性>')}")
    if not gw._connected:
        print("[FAIL] 连接未成功，终止。")
        return

    # --- 步骤 2：query_asset（T2，真实资产 4 字段）---
    if _step("步骤 2: query_asset（T2 · 期望 {account_id, cash, total_asset, market_value}）"):
        return
    asset = await gw.query_asset()
    print(f"  资产：{asset}")
    _ok(set(asset.keys()) >= {"cash", "total_asset", "market_value"} if asset else False,
        f"4 字段齐（cash/total_asset/market_value）：{bool(asset)}")
    _ok(isinstance(asset.get("total_asset"), (int, float)) if asset else False,
        f"total_asset 是数值（二期熔断 equity 源）：{asset.get('total_asset')}")

    # --- 步骤 3：query_positions（既有 _fetch_broker_positions）---
    if _step("步骤 3: query_positions（既有 · 期望 {symbol: volume} 持仓，空也 OK）"):
        return
    positions = await gw._fetch_broker_positions()
    print(f"  持仓（{len(positions)} 只）：{positions}")
    _ok(isinstance(positions, dict), f"返 dict：{isinstance(positions, dict)}")

    # --- 步骤 4：query_orders（T4，当日委托）---
    if _step("步骤 4: query_orders（T4 · 当日委托 list[dict]，空也 OK）"):
        return
    orders = await gw.query_orders()
    print(f"  当日委托（{len(orders)} 笔）：")
    for o in orders[:5]:
        print(f"    {o.get('stock_code')} state={o.get('state')} oid={o.get('order_id')} "
              f"vol={o.get('order_volume')} traded={o.get('traded_volume')}")
    _ok(isinstance(orders, list), f"返 list：{isinstance(orders, list)}")
    if orders:
        state0 = orders[0].get("state")
        _ok(hasattr(state0, "name"), f"state 是 OrderState 枚举（非字符串）：{state0}")

    # --- 步骤 5：query_trades（T4，当日成交）---
    if _step("步骤 5: query_trades（T4 · 当日成交 list[dict]，空也 OK）"):
        return
    trades = await gw.query_trades()
    print(f"  当日成交（{len(trades)} 笔）：")
    for t in trades[:5]:
        print(f"    {t.get('stock_code')} vol={t.get('traded_volume')} price={t.get('traded_price')}")
    _ok(isinstance(trades, list), f"返 list：{isinstance(trades, list)}")

    # --- 步骤 6：get_quote（T3，单只行情）---
    sym = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH").split(",")[0].strip()
    if _step(f"步骤 6: get_quote（T3 单只 · {sym} 期望 last_price/high_limit/low_limit）"):
        return
    quote = await qmt_market_data.get_quote(sym)
    print(f"  {sym} 快照：{quote}")
    _ok(quote is not None and "last_price" in (quote or {}),
        f"含 last_price：{quote.get('last_price') if quote else None}")
    _ok(quote is not None and "high_limit" in (quote or {}),
        f"含 high_limit（risk_shield 第9关涨跌停用）：{quote.get('high_limit') if quote else None}")

    # --- 步骤 7：get_quotes（T3，批量行情）---
    syms_raw = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH,159915.SZ").split(",")
    syms = [s.strip() for s in syms_raw if s.strip()][:5]
    if _step(f"步骤 7: get_quotes（T3 批量 · {syms} · 一次 get_full_tick 取多只）"):
        return
    quotes = await qmt_market_data.get_quotes(syms)
    print(f"  批量行情（{len(quotes)} 只）：")
    for s, q in quotes.items():
        lp = q.get("last_price") if q else None
        print(f"    {s}: last_price={lp} {'(缺失→None)' if q is None else ''}")
    _ok(set(quotes.keys()) == set(syms), f"返回键 = 请求键：{set(quotes.keys()) == set(syms)}")
    _ok(all(v is None or "last_price" in v for v in quotes.values()),
        "每只值 None 或含 last_price：True")

    # --- 步骤 8：_sync_orders_if_stale（T5，惰性同步）---
    if _step(f"步骤 8: _sync_orders_if_stale（T5 · _main_push_available={gw._main_push_available}）"):
        return
    n = await gw._sync_orders_if_stale()
    print(f"  同步笔数：{n}（_main_push_available={gw._main_push_available}）")
    if gw._main_push_available:
        _ok(n == 0, "主推可用 → no-op 返 0：True")
        print("  （subscribe 成功，主推正常，惰性同步不触发——符合预期）")
    else:
        _ok(n >= 0, f"主推不可用 → 主动 query_orders 同步 {n} 笔：True")

    # --- 步骤 9：on_account_status 观察（T1，被动等推送）---
    if _step("步骤 9: on_account_status 观察（T1 · 等 3s 看有无账号状态推送，"
             "正常账号 OK 态可能不推；_lock_down 应保持 False）"):
        return
    locked_before = gw.is_locked
    print(f"  等待 3s 观察账号状态推送（正常 OK 账号可能静默无推送，属预期）...")
    await asyncio.sleep(3)
    locked_after = gw.is_locked
    _ok(not locked_after, f"等待后 _lock_down={locked_after}（应保持 False，除非账号真异常）")
    if locked_after and not locked_before:
        print("  [!] 账号状态推送触发锁定——检查模拟盘账号是否被停用/登录失败")

    # --- 步骤 10：真单 + 撤单 + 回调推送观察（既有 submit/cancel + 回调链路）---
    ans = input("\n步骤 10: 真实最小限价单（模拟盘，100 股，直连网关不经挡板）+ 撤单 + "
                "观察 on_stock_order/on_stock_trade/on_order_stock_async_response 推送。"
                "\n输入 YES 继续，其他跳过：").strip()
    if ans != "YES":
        print("已跳过真单步骤。")
        await gw.disconnect()
        _summary()
        return
    print(f"[!] 发起真实限价单 100 股 {sym} @5.0（模拟盘）...")
    order = OrderRequest(symbol=sym, qty=100, side="buy", price=5.0)
    result = await gw.submit_order(order)
    print(f"  下单结果：order_id={result.order_id} state={result.state.name} msg={result.message}")
    _ok(result.state == OrderState.SUBMITTED, f"submit → SUBMITTED：{result.state == OrderState.SUBMITTED}")
    if result.state == OrderState.SUBMITTED:
        print("  等待 2s 供 on_order_stock_async_response 建立 seq→real 映射...")
        await asyncio.sleep(2)
        print(f"  期间推送计数：{_push_count}")
        cancel_res = await gw.cancel_order(result.order_id)
        print(f"  撤单结果：state={cancel_res.state.name} msg={cancel_res.message}")
        _ok("on_stock_order" in cancel_res.message, "cancel message 含非终态语义：True")
        # 撤单后等推送
        print("  等待 2s 观察 on_stock_order 推 CANCELLED...")
        await asyncio.sleep(2)

    await gw.disconnect()
    _summary()


def _summary():
    """打印推送计数汇总 + 通过情况。"""
    print(f"\n{'=' * 64}\n=== 回调推送汇总\n{'=' * 64}")
    print(f"  推送计数：{_push_count}")
    print("\n联调完成。请核对：")
    print("  - 各步骤 ✅/❌ 通过情况")
    print("  - miniQMT 客户端委托/成交记录（步骤 10 真单+撤单）")
    print("  - 若有 ❌，对照 T1-T6 接口契约排查（见 scripts/qmt_live_smoke.py docstring）")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户退出。")
