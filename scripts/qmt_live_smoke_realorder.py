# -*- coding: utf-8 -*-
"""miniQMT 真单 + 撤单 headless 联调（模拟仓 · 验证 submit/cancel/回调推送全链路）。

物理意图：scripts/qmt_live_smoke_headless.py 只跑只读接口；本脚本补「真单 + 撤单 +
回调推送」最后一环（原 qmt_live_smoke.py 步骤10 的自动版）。

安全设计（CLAUDE.md 模拟仓无顾忌但仍守序 + 风控拷问）：
  - 小额：100 股（A 股最小委托单位）。
  - 挂【远离盘口】的限价买单：price = min(现价×0.95, 现价-0.3)，且 ≥ 跌停+0.01。
    原交互脚本固定 price=5.0 对现价 4.786 是「挂高于现价」→ 立即成交，撤单来不及，
    测不了撤单链路；本脚本确保挂单【不成交】，能干净撤掉。
  - 限价单（非市价），价格完全可控；模拟盘无真实资金风险。
  - 撤单后 query_orders 复核终态 = CANCELLED（防撤单链路静默失败）。

验证矩阵：
  submit_order        — OrderRequest → SubmitResult{order_id, state=SUBMITTED, message}
  on_order_stock_async_response — seq→real 映射建立（gw 内部，体现在 order_id 有效）
  cancel_order        — CancelResult{state, message}
  on_stock_order 推送 — CANCELLED 终态推送（主推回调链路）
  query_orders 复核   — 当日委托该单 state=OrderState.CANCELLED
"""
import asyncio
import os
import sys
import traceback

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from trading.qmt_gateway import QmtExecutionGateway
from trading import qmt_market_data
from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：垫片已删，直指 compute.types 真身
from trading.order_state import OrderState

_results = []
_push = {"order": 0, "trade": 0, "order_error": 0, "cancel_error": 0, "async_response": 0}
_last_states = []  # 记录该单推送过的 state 序列
_real_oid = [None]  # 推送回调里捕获的【真实柜台 order_id】（submit 返的是内部 seq）


async def _on_order_update(update: dict) -> None:
    kind = update.get("kind", "?")
    _push[kind] = _push.get(kind, 0) + 1
    state = update.get("state")
    sname = state.name if hasattr(state, "name") else state
    oid = update.get("order_id")
    if oid and _real_oid[0] is None:
        _real_oid[0] = oid  # 首条推送即真实柜台 oid（async_response 补全后的）
    print(f"  [推送#{_push[kind]}] kind={kind} sym={update.get('stock_code')} "
          f"state={sname} oid={oid} traded={update.get('traded_volume', '')}")
    if kind in ("order", "async_response") and state is not None:
        _last_states.append(sname)


def _record(tag: str, ok: bool, detail: str = "") -> None:
    _results.append((tag, ok))
    print(f"  [{'✅' if ok else '❌'}] {tag}" + (f"  | {detail}" if detail else ""))


async def main():
    sym = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH").split(",")[0].strip()
    qty = 100
    print("=" * 64)
    print(f"miniQMT 真单+撤单联调（模拟仓 · {sym} · {qty}股 · 限价买单远离盘口）")
    print("=" * 64)

    gw = QmtExecutionGateway()
    gw.set_order_update_callback(_on_order_update)

    try:
        await gw.connect()
    except Exception as e:
        _record("connect", False, f"异常 {type(e).__name__}: {e}")
        return
    if not gw._connected:
        _record("connect", False, "连接未成功")
        return
    _record("connect", True, f"_connected is_locked={gw.is_locked}")

    # 1) 取实时现价 + 涨跌停，算安全挂单价
    quote = await qmt_market_data.get_quote(sym)
    if not quote or not quote.get("last_price"):
        _record("取现价", False, f"get_quote 无 last_price：{quote}")
        await gw.disconnect()
        return
    last = quote["last_price"]
    low_limit = quote.get("low_limit")
    high_limit = quote.get("high_limit")
    # 买单远离盘口：min(现价×0.95, 现价-0.3)，且不破跌停（破跌停是废单）
    raw_safe = min(last * 0.95, last - 0.3)
    floor = (low_limit + 0.01) if low_limit else raw_safe
    safe_price = round(max(raw_safe, floor), 3)
    _record("安全挂单价计算", True,
            f"last={last} low_limit={low_limit} high_limit={high_limit} → 挂买价={safe_price}")
    if safe_price >= last:
        _record("安全价校验", False, f"挂买价 {safe_price} ≥ 现价 {last}，会成交，终止")
        await gw.disconnect()
        return
    _record("安全价校验（挂买价<现价，不成交）", True)

    # 2) submit_order
    try:
        order = OrderRequest(symbol=sym, qty=qty, side="buy", price=safe_price)
        result = await gw.submit_order(order)
    except Exception as e:
        _record("submit_order", False, f"异常 {type(e).__name__}: {e}")
        await gw.disconnect()
        return
    print(f"  下单结果：order_id={result.order_id} state={result.state.name} msg={result.message}")
    _record("submit → SUBMITTED", result.state == OrderState.SUBMITTED,
            f"state={result.state.name} oid={result.order_id}")
    if result.state != OrderState.SUBMITTED or not result.order_id:
        print("[FAIL] 下单未进入 SUBMITTED，终止（不发撤单）。")
        await gw.disconnect()
        _summary()
        return

    # 3) 等 on_order_stock_async_response 建立 seq→real 映射
    print("  等 2s 供 on_order_stock_async_response 建立 seq→real 映射...")
    await asyncio.sleep(2)
    _record("async_response 推送到达", _push["async_response"] >= 1,
            f"推送={_push}")

    # 4) cancel_order
    try:
        cancel_res = await gw.cancel_order(result.order_id)
    except Exception as e:
        _record("cancel_order", False, f"异常 {type(e).__name__}: {e}")
        await gw.disconnect()
        _summary()
        return
    print(f"  撤单结果：state={cancel_res.state.name} msg={cancel_res.message}")
    _record("cancel_order 调用成功", cancel_res.state in (OrderState.CANCELLED, OrderState.SUBMITTED),
            f"state={cancel_res.state.name}")
    _record("cancel message 含非终态语义", "on_stock_order" in (cancel_res.message or ""),
            f"msg={cancel_res.message}")

    # 5) 等 on_stock_order 推 CANCELLED 终态
    print("  等 3s 观察 on_stock_order 推 CANCELLED 终态...")
    await asyncio.sleep(3)
    cancelled_pushed = any("CANCEL" in str(s).upper() for s in _last_states)
    _record("收到 CANCELLED 推送", cancelled_pushed,
            f"推送state序列={_last_states} push={_push}")

    # 6) query_orders 复核终态
    try:
        orders = await gw.query_orders()
    except Exception as e:
        _record("query_orders 复核", False, f"异常 {type(e).__name__}: {e}")
        orders = []
    # query_orders 返回【真实柜台 order_id】（非 submit 的内部 seq），
    # 用推送回调捕获的真实 oid 复核；seq→real 映射是 gw 内部细节，对账须用 real id。
    match_oid = _real_oid[0] or result.order_id
    mine = [o for o in orders if o.get("order_id") == match_oid] if orders else []
    if mine:
        s = mine[0].get("state")
        sname = s.name if hasattr(s, "name") else s
        _record("query_orders 终态=CANCELLED", s == OrderState.CANCELLED,
                f"state={sname} traded={mine[0].get('traded_volume')}")
    else:
        _record("query_orders 复核", False, f"委托列表未找到该单 oid={result.order_id}（共{len(orders)}笔）")

    await gw.disconnect()
    _summary()


def _summary():
    print("\n" + "=" * 64)
    print("=== 真单+撤单联调结果")
    print("=" * 64)
    for tag, ok in _results:
        print(f"  [{'✅' if ok else '❌'}] {tag}")
    passed = sum(1 for _t, ok in _results if ok)
    print(f"\n  总计：{passed}/{len(_results)} 通过")
    print("  推送计数：", _push)
    print("  请到 miniQMT 客户端核对：委托记录应有该买单 + 已撤，无成交。")
    print("=" * 64)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户退出。")
    except Exception:
        traceback.print_exc()
