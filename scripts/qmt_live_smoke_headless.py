# -*- coding: utf-8 -*-
"""miniQMT headless 只读联调（非交互 · 跑全部只读接口 · 不发真单）。

物理意图：scripts/qmt_live_smoke.py 的自动跑兄弟版。原脚本每步 input() 人工确认，
适合人工把守的真单链路；本脚本去掉交互，一次性把 T1-T6 + 既有只读接口在真实模拟盘
上端到端跑通，输出 ✅/❌ 矩阵，便于一眼扫「哪些接口当前模拟盘调用成功」。

铁律（CLAUDE.md 模拟仓无顾忌但仍守序）：
  - 严格只读：connect / query_asset / positions / query_orders / query_trades /
    get_quote / get_quotes / _sync_orders_if_stale / 被动等 on_account_status。
  - 不发任何 submit_order / cancel_order（真单走原交互脚本 qmt_live_smoke.py 步骤10）。
  - 所有调用 try/except 包裹，单接口失败不阻断后续，给出失败原因供排查。

运行：.venv310/Scripts/python.exe scripts/qmt_live_smoke_headless.py
"""
import asyncio
import os
import sys
import traceback

# Windows 控制台默认 GBK，✅/❌/中文会 UnicodeEncodeError——强制 stdout/stderr 走 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from trading.qmt_gateway import QmtExecutionGateway
from trading import qmt_market_data


# === 结果矩阵（每个接口一条）===================================================
_results = []  # [(iface, tag, ok, detail)]
_push_count = {"order": 0, "trade": 0, "order_error": 0, "cancel_error": 0, "async_response": 0}


async def _on_order_update(update: dict) -> None:
    """订单更新回调：计数（只读联调期间不应有推送，除非之前有未完成委托）。"""
    kind = update.get("kind", "?")
    _push_count[kind] = _push_count.get(kind, 0) + 1


def _record(iface: str, tag: str, ok: bool, detail: str = "") -> None:
    _results.append((iface, tag, ok, detail))
    mark = "✅" if ok else "❌"
    line = f"  [{mark}] {iface} :: {tag}"
    if detail:
        line += f"  | {detail}"
    print(line)


def _safe(iface: str, tag: str, coro_fn, *args, **kwargs):
    """统一 try/except 包装：异常也记 ❌ + 原因，不阻断。返回协程。"""

    async def _run():
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:  # 单接口失败不阻断后续
            _record(iface, tag, False, f"异常 {type(e).__name__}: {e}")
            return None

    return _run()


async def main():
    print("=" * 64)
    print("miniQMT headless 只读联调启动（模拟仓 · T1-T6 · 不发真单）")
    print(f"account={os.getenv('QMT_ACCOUNT_ID')}  userdata={os.getenv('QMT_USERDATA_PATH')}")
    print(f"session={os.getenv('QMT_SESSION_ID', '123456')}")
    print("=" * 64)

    gw = QmtExecutionGateway()
    gw.set_order_update_callback(_on_order_update)

    # --- 步骤1：connect（既有 + subscribe + _main_push_available）---
    try:
        await gw.connect()
    except Exception as e:
        _record("connect", "连接 miniQMT 网关", False, f"异常 {type(e).__name__}: {e}")
        _summary()
        return
    _record("connect", "_connected=True", gw._connected, f"_connected={gw._connected}")
    _record("connect", "is_locked=False（账号未锁）", not gw.is_locked, f"is_locked={gw.is_locked}")
    mpa = getattr(gw, "_main_push_available", None)
    _record("connect", "_main_push_available=True（主推可用）", mpa is True,
            f"_main_push_available={mpa}")
    if not gw._connected:
        print("[FAIL] 连接未成功，终止。")
        _summary()
        return

    # --- 步骤2：query_asset（T2）---
    asset = await _safe("query_asset", "真实资产返回", gw.query_asset)
    if asset is not None:
        keys_ok = isinstance(asset, dict) and {"cash", "total_asset", "market_value"} <= set(asset.keys())
        _record("query_asset", "4字段齐(cash/total_asset/market_value)", keys_ok,
                f"keys={sorted(asset.keys()) if isinstance(asset, dict) else type(asset).__name__}")
        ta = asset.get("total_asset") if isinstance(asset, dict) else None
        _record("query_asset", "total_asset 是数值（二期熔断 equity 源）",
                isinstance(ta, (int, float)),
                f"total_asset={ta}  cash={asset.get('cash')}  mv={asset.get('market_value')}")

    # --- 步骤3：query_positions（既有）---
    positions = await _safe("query_positions", "持仓 dict 返回", gw._fetch_broker_positions)
    if positions is not None:
        _record("query_positions", "返 dict（空也 OK）", isinstance(positions, dict),
                f"{len(positions)} 只：{dict(list(positions.items())[:3])}" if positions else "空持仓")

    # --- 步骤4：query_orders（T4 当日委托）---
    orders = await _safe("query_orders", "当日委托 list 返回", gw.query_orders)
    if orders is not None:
        _record("query_orders", "返 list（空也 OK）", isinstance(orders, list),
                f"{len(orders)} 笔")
        if orders:
            state0 = orders[0].get("state")
            _record("query_orders", "state 是 OrderState 枚举（非字符串）",
                    hasattr(state0, "name"), f"首单 state={state0}")

    # --- 步骤5：query_trades（T4 当日成交）---
    trades = await _safe("query_trades", "当日成交 list 返回", gw.query_trades)
    if trades is not None:
        _record("query_trades", "返 list（空也 OK）", isinstance(trades, list),
                f"{len(trades)} 笔")

    # --- 步骤6：get_quote（T3 单只）---
    sym = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH").split(",")[0].strip()
    quote = await _safe("get_quote", f"单只快照 {sym}", qmt_market_data.get_quote, sym)
    if quote is not None:
        _record("get_quote", "含 last_price", isinstance(quote, dict) and "last_price" in quote,
                f"last_price={quote.get('last_price')}" if isinstance(quote, dict) else f"非dict:{quote}")
        _record("get_quote", "含 high_limit（risk_shield 第9关涨跌停用）",
                isinstance(quote, dict) and "high_limit" in quote,
                f"high_limit={quote.get('high_limit')} low_limit={quote.get('low_limit')}" if isinstance(quote, dict) else "")

    # --- 步骤7：get_quotes（T3 批量）---
    syms_raw = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH,159915.SZ").split(",")
    syms = [s.strip() for s in syms_raw if s.strip()][:5]
    quotes = await _safe("get_quotes", f"批量快照 {syms}", qmt_market_data.get_quotes, syms)
    if quotes is not None:
        keys_eq = isinstance(quotes, dict) and set(quotes.keys()) == set(syms)
        _record("get_quotes", "返回键 = 请求键", keys_eq,
                f"keys={sorted(quotes.keys()) if isinstance(quotes, dict) else type(quotes).__name__}")
        if isinstance(quotes, dict):
            sample = {s: (q.get("last_price") if isinstance(q, dict) else None) for s, q in list(quotes.items())[:5]}
            _record("get_quotes", "每只 None 或含 last_price",
                    all(q is None or (isinstance(q, dict) and "last_price" in q)
                        for q in quotes.values()),
                    f"last_price={sample}")

    # --- 步骤8：_sync_orders_if_stale（T5 惰性同步）---
    n = await _safe("_sync_orders_if_stale", "惰性同步返回 int", gw._sync_orders_if_stale)
    if n is not None:
        if getattr(gw, "_main_push_available", None) is True:
            _record("_sync_orders_if_stale", "主推可用→no-op 返 0", n == 0, f"sync={n}")
        else:
            _record("_sync_orders_if_stale", "主推不可用→主动 query 同步", isinstance(n, int) and n >= 0,
                    f"sync={n}")

    # --- 步骤9：on_account_status 被动观察（T1）---
    locked_before = gw.is_locked
    await asyncio.sleep(3)  # 等 3s 看有无账号状态推送（正常 OK 账号可能静默）
    locked_after = gw.is_locked
    _record("on_account_status", "等待3s后账号未锁（_lock_down=False）", not locked_after,
            f"locked {locked_before}->{locked_after}  push={_push_count}")

    await gw.disconnect()
    _summary()


def _summary():
    print("\n" + "=" * 64)
    print("=== 联调结果矩阵")
    print("=" * 64)
    by_iface = {}
    for iface, tag, ok, _d in _results:
        by_iface.setdefault(iface, []).append(ok)
    for iface, oks in by_iface.items():
        passed = sum(oks)
        total = len(oks)
        mark = "✅" if passed == total else "⚠️"
        print(f"  {mark} {iface:24s} {passed}/{total}")
    total_pass = sum(1 for _i, _t, ok, _d in _results if ok)
    total_all = len(_results)
    print(f"\n  总计：{total_pass}/{total_all} 通过")
    print("  推送计数（只读期间应为全 0）：", _push_count)
    print("=" * 64)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户退出。")
    except Exception:
        traceback.print_exc()
