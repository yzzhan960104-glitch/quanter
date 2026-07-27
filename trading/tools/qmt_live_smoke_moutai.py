# -*- coding: utf-8 -*-
"""miniQMT 茅台实盘冒烟测试（模拟盘 · 挂单/下单/撤单/成交全链路 · 三源交叉验证）。

物理意图：趁开盘时段，用贵州茅台 600519.SH 真连 miniQMT 模拟盘柜台，端到端验证
QmtExecutionGateway（broker/qmt.py:225）的挂单→下单→撤单→成交链路 + 回调推送。
核心标尺：真实请求 + 三源交叉验证（杜绝本地「自嗨」式成功）。

【seq ↔ real_oid 双 ID 对账】（2026-07-27 实测发现并修复）
  - submit_order 返回 order_id = str(seq)（本地序号，如 "4"/"13"）
  - 柜台主推 / query_orders / gw._orders 用 real_oid（柜台真实单号，如 1048577）
  - 两者由 on_order_stock_async_response 回调建立的 gw._seq_to_real[seq]=real_oid 映射
  - 故对账（query_orders / gw._orders）必须用 real_oid；cancel_order 用 seq（网关内部转）
  - 首版脚本用 seq 对账致「查不到 → 误判未撤 → 误重试 → cancel_error」，本版修复

铁律（CLAUDE.md 模拟仓无顾忌但仍守序）：
  - AUTO 模式（QMT_SMOKE_AUTO=1）跳过 input 直跑串通自动化；交互模式每步 input 把守。
  - 三源交叉验证：源①函数返回(seq) / 源②gw._orders 主推流水(real_oid) / 源③query_orders
    主动复核(real_oid)，三者一致才 ✅，任一缺失/不一致 ❌ 告警，绝不靠单方面返回值宣布成功。
  - 挂撤段挂跌停价买单（不成交）；成交段挂涨停价买单（即成）。
  - 撤单以源②/源③终态为准（非 cancel 返回值），未到 CANCELLED 才重试，杜绝「cancel 返成功
    但柜台没撤」的自嗨；不留可撤未撤废单。

边界（已核实）：
  - _fetch_broker_positions 过滤 can_use==0 → 当日买入（T+1）不可见，成交段用原始 query_stock_positions。
  - LATEST_PRICE 市价单模拟环境不支持，成交段用涨停价限价单替代。

运行：
  AUTO：QMT_SMOKE_AUTO=1 .venv310/Scripts/python.exe trading/tools/qmt_live_smoke_moutai.py
  交互：.venv310/Scripts/python.exe trading/tools/qmt_live_smoke_moutai.py
"""
import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 脚本位于 项目根/trading/tools/，须三层 dirname 上溯到项目根（如 F:\quanter）。
# ⚠️ 两层 dirname 只到 trading/，会被 sys.path.insert 进 sys.path[0]，致顶层 `import calendar`
# 命中 trading/calendar.py（无 day_abbr）→ pandas→_strptime 崩，且 `from trading` 找不到包。
# 既有 qmt_live_smoke.py 等潜伏此 bug（两层 dirname，从未真跑过）。三层 dirname 从根上修复，
# 无需预 import calendar（之前误判 trading 包动态污染，实为 dirname 层数错）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from trading.qmt_gateway import QmtExecutionGateway
from trading import qmt_market_data
from trading.compute.types import OrderRequest
from trading.types.order_state import OrderState

SYMBOL = "600519.SH"
QTY = 100

AUTO = os.environ.get("QMT_SMOKE_AUTO", "") == "1"

# === 源②柜台主推观察 ===
_push_count = {"order": 0, "trade": 0, "order_error": 0, "cancel_error": 0, "async_response": 0}
_last_status_msg = {}  # real_oid(str) -> 最近一条 status_msg（REJECTED 等原因捕获用）


async def _on_order_update(update: dict) -> None:
    """订单更新回调（主线程 create_task 调度）。逐条打印 + 按 kind 计数 + 记 status_msg。"""
    kind = update.get("kind", "?")
    _push_count[kind] = _push_count.get(kind, 0) + 1
    sym = update.get("stock_code", "")
    state = update.get("state")
    state_name = state.name if hasattr(state, "name") else state
    oid = update.get("order_id")
    smsg = update.get("status_msg") or update.get("order_remark") or ""
    if oid and smsg:
        _last_status_msg[str(oid)] = smsg  # 供后续对账查 REJECTED 原因
    line = (f"  [推送#{_push_count[kind]}] kind={kind} sym={sym} state={state_name} "
            f"oid={oid} traded={update.get('traded_volume', '')} price={update.get('traded_price', '')}")
    if smsg:
        line += f" msg={smsg}"
    print(line)


def _step(title: str) -> bool:
    print(f"\n{'=' * 64}\n=== {title}\n{'=' * 64}")
    if AUTO:
        return False
    return input("回车继续，q 退出：").strip().lower() == "q"


def _gate(prompt: str) -> bool:
    print(f"\n[真单把守] {prompt}")
    if AUTO:
        print("  [AUTO] 自动放行（模拟仓，跳过交互）")
        return True
    return input("输入 YES 继续，其他跳过：").strip() == "YES"


def _ok(cond: bool, label: str) -> None:
    print(f"  [{'✅' if cond else '❌'}] {label}")


def _state_name(s) -> str:
    return s.name if hasattr(s, "name") else str(s)


# === seq → real_oid 解析 + 三源查询 ======================================
async def _resolve_real_oid(gw, seq_str, max_polls=60):
    """seq → real_oid：轮询 gw._seq_to_real（async_response 回调填）。

    submit 返 str(seq)，柜台主推/query_orders/gw._orders 用 real_oid，对账前必须映射。
    async_response 在 submit 后异步到达（实测 <500ms），轮询等（每 50ms，最多 3s）。
    """
    try:
        seq = int(seq_str)
    except (ValueError, TypeError):
        return None
    for _ in range(max_polls):  # 60 × 50ms = 3s
        real = gw._seq_to_real.get(seq)
        if real is not None:
            return real
        await asyncio.sleep(0.05)
    return None


def _read_push_order(gw, real_oid):
    """源②：从 gw._orders 主推流水读订单最新状态（key=str(real_oid)）。

    gw._orders 由 on_stock_order 回调实时写入，比 query_orders 更贴近主推当下态。
    返 dict（含 state/traded_volume/status_msg 等）或 None。
    """
    if real_oid is None:
        return None
    return gw._orders.get(str(real_oid))


async def _query_order(gw, real_oid, cancelable_only=False):
    """源③：query_orders 主动查询，定位 real_oid 对应委托。返 dict 或 None。"""
    if real_oid is None:
        return None
    orders = await gw.query_orders(cancelable_only=cancelable_only)
    for o in orders:
        if str(o.get("order_id")) == str(real_oid):
            return o
    return None


def _dump_order(src, rec):
    """统一打印一条订单记录（源②/源③通用）。"""
    if not rec:
        print(f"  {src}: <未查到>")
        return
    state = rec.get("state")
    print(f"  {src}: state={_state_name(state)} traded={rec.get('traded_volume', rec.get('traded', 0))} "
          f"price={rec.get('traded_price', rec.get('price', ''))}"
          + (f" msg={rec.get('status_msg')}" if rec.get("status_msg") else ""))


# === 撤单（cancel 用 seq；终态校验用 real_oid）=========================
async def _wait_terminal(gw, real_oid, timeout_s=3.0):
    """轮询等订单到终态（CANCELLED/FILLED/PARTIAL_FILLED/REJECTED），返终态记录或 None。

    Why 轮询而非固定 sleep：cancel 后 on_stock_order 推 CANCELLED 的到达时延不定
    （实测 1~2s），固定 sleep 1.5s 会在推送到达前判定，误判「未撤」触发重试→cancel_error。
    轮询以 gw._orders 主推流水为准（最实时），超时再 fallback 主动 query_orders。"""
    import time as _t
    deadline = _t.time() + timeout_s
    terminal = (OrderState.CANCELLED, OrderState.FILLED, OrderState.PARTIAL_FILLED, OrderState.REJECTED)
    while _t.time() < deadline:
        rec = _read_push_order(gw, real_oid)
        if rec and rec.get("state") in terminal:
            return rec
        await asyncio.sleep(0.3)
    return await _query_order(gw, real_oid, cancelable_only=False)  # 超时 fallback 主动查


async def _cancel_with_retry(gw, seq_str, real_oid):
    """撤单：cancel_order 用 seq（网关内部 seq→real），终态校验用 real_oid（轮询等）。

    以源② gw._orders 主推终态为准（非 cancel 返回值），杜绝自嗨。cancel 后轮询 3s 等
    CANCELLED 到达；超时未到才重试 1 次。已到 FILLED/REJECTED 等不可撤终态 → 不重试。
    """
    res = None
    for attempt in (1, 2):
        try:
            res = await gw.cancel_order(seq_str)  # cancel 用 seq
            print(f"  源① cancel(第{attempt}次) → state={res.state.name} msg={res.message}")
        except Exception as e:
            print(f"  撤单异常(第{attempt}次) {type(e).__name__}：{e}")
            res = None
        rec = await _wait_terminal(gw, real_oid, timeout_s=3.0)
        if not rec:
            print(f"  第{attempt}次后源②③均未查到 real_oid={real_oid}（主推/查询延迟）")
            if attempt == 1:
                continue
            break
        s = rec.get("state")
        traded = rec.get("traded_volume", 0) or 0
        if s == OrderState.CANCELLED:
            print(f"  源②③ 确认终态 CANCELLED ✅（traded={traded}）")
            return res
        if s in (OrderState.FILLED, OrderState.PARTIAL_FILLED, OrderState.REJECTED):
            print(f"  [!] 终态={_state_name(s)}（成交/拒单，撤不动）traded={traded} "
                  f"msg={rec.get('status_msg','')}")
            return res
        if attempt == 1:
            print(f"  未到终态（当前 {_state_name(s)}），重试撤单...")
    print("  [!] 撤单重试后仍未 CANCELLED —— 请人工核对 miniQMT 客户端是否残留废单！")
    return res


# === 挂撤段（跌停价买单，不成交）=========================================
async def phase_limit_no_fill(gw, low_limit: float):
    print(f"\n[挂撤段] 挂买单 {QTY} 股 @跌停价 {low_limit}（远离盘口，预期不成交）")
    if not _gate(f"挂买单 {SYMBOL} {QTY}股 @跌停价{low_limit}（模拟盘，不成交预期）"):
        print("  已跳过挂撤段。")
        return None

    push_before = _push_count["order"]
    order = OrderRequest(symbol=SYMBOL, qty=QTY, side="buy", price=low_limit)
    result = await gw.submit_order(order)  # 源①（返 seq）
    print(f"  源① submit → order_id(seq)={result.order_id} state={result.state.name} msg={result.message}")
    _ok(result.state == OrderState.SUBMITTED, f"源① submit→SUBMITTED：{result.state == OrderState.SUBMITTED}")
    if result.state != OrderState.SUBMITTED:
        print("  [!] 挂单未提交成功，终止挂撤段。")
        return result

    print("  等 2s 供 async_response 建 seq→real 映射 + on_stock_order 首推...")
    await asyncio.sleep(2)
    real_oid = await _resolve_real_oid(gw, result.order_id)
    print(f"  seq={result.order_id} → real_oid={real_oid}")
    _ok(_push_count["order"] > push_before,
        f"源② on_stock_order 主推到达：+{_push_count['order'] - push_before} 条")

    # 源②③ 对账（用 real_oid）
    rec_push = _read_push_order(gw, real_oid)
    rec_q = await _query_order(gw, real_oid, cancelable_only=True)
    _dump_order("源② gw._orders", rec_push)
    _dump_order("源③ query_orders(cancelable)", rec_q)

    print("  发起撤单...")
    push_before_cancel = _push_count["order"]  # cancel 前记基线（CANCELLED 推送在 cancel 后到）
    await _cancel_with_retry(gw, result.order_id, real_oid)  # cancel 用 seq，校验用 real
    await asyncio.sleep(1)  # 给延迟推送留窗口
    _ok(_push_count["order"] > push_before_cancel,
        f"源② 撤单主推到达：+{_push_count['order'] - push_before_cancel} 条（含 CANCELLED）")
    return result


# === 成交段（涨停价买单，即成）===========================================
async def phase_limit_fill(gw, high_limit: float):
    print(f"\n[成交段] 挂买单 {QTY} 股 @挂单价 {high_limit}（卖一价即成预期，实价≈挂单价）")
    asset_before = await gw.query_asset()
    raw_before = await _raw_positions(gw)
    mtai_before = raw_before.get(SYMBOL, {})
    cash_before = asset_before.get("cash") if asset_before else None
    mv_before = asset_before.get("market_value") if asset_before else None
    vol_before = mtai_before.get("volume", 0)
    print(f"  基线：cash={cash_before} mv={mv_before} 茅台volume={vol_before}")

    if not _gate(f"挂买单 {SYMBOL} {QTY}股 @{high_limit}（即成，T+1留仓100股）"):
        print("  已跳过成交段。")
        return None

    push_o_before = _push_count["order"]
    push_t_before = _push_count["trade"]
    order = OrderRequest(symbol=SYMBOL, qty=QTY, side="buy", price=high_limit)
    result = await gw.submit_order(order)  # 源①
    print(f"  源① submit → order_id(seq)={result.order_id} state={result.state.name} msg={result.message}")
    if result.state != OrderState.SUBMITTED:
        print("  [!] 成交单未提交成功，终止成交段。")
        return result

    print("  等 3s 供撮合 + async_response + on_stock_order/on_stock_trade 主推...")
    await asyncio.sleep(3)
    print(f"  推送：order +{_push_count['order'] - push_o_before}，trade +{_push_count['trade'] - push_t_before}")
    real_oid = await _resolve_real_oid(gw, result.order_id)
    print(f"  seq={result.order_id} → real_oid={real_oid}")

    # 源②③ 委托终态（用 real_oid）
    rec_push = _read_push_order(gw, real_oid)
    rec_q = await _query_order(gw, real_oid, cancelable_only=False)
    _dump_order("源② gw._orders", rec_push)
    _dump_order("源③ query_orders", rec_q)
    rec = rec_push or rec_q
    if rec:
        s = rec.get("state")
        traded = rec.get("traded_volume", 0) or 0
        _ok(s == OrderState.FILLED, f"源②③ 终态==FILLED：{s == OrderState.FILLED}")
        if s == OrderState.REJECTED:
            msg = rec.get("status_msg") or _last_status_msg.get(str(real_oid), "")
            print(f"  [!] 成交单被柜台 REJECTED！status_msg={msg}（据此定位拒因）")
        if 0 < traded < QTY:
            print(f"  [!] 部分成交 traded={traded}<{QTY} —— 暴露部分成交精度 gap，如实记录")
        _ok(traded == QTY, f"源②③ 全成 traded=={QTY}：{traded == QTY}")
    else:
        print("  [!] 源②③ 均未查到 real_oid")

    # 源② 成交主推
    _ok(_push_count["trade"] > push_t_before,
        f"源② on_stock_trade 主推：+{_push_count['trade'] - push_t_before} 条")

    # 源③ 成交明细
    trades = await gw.query_trades()
    mine = [t for t in trades if str(t.get("order_id")) == str(real_oid)] if real_oid else []
    if mine:
        t0 = mine[-1]
        print(f"  源③ query_trades → 实价={t0.get('traded_price')} 量={t0.get('traded_volume')} 额={t0.get('traded_amount')}")
    elif real_oid:
        print(f"  [!] query_trades 未查到 real_oid={real_oid} 成交（共 {len(trades)} 笔）")

    # 源③ 资产变化
    asset_after = await gw.query_asset()
    cash_after = asset_after.get("cash") if asset_after else None
    mv_after = asset_after.get("market_value") if asset_after else None
    if cash_before is not None and cash_after is not None:
        print(f"  源③ 资产 cash {cash_before:.2f}→{cash_after:.2f}（↓{cash_before - cash_after:.2f}）")
    if mv_before is not None and mv_after is not None:
        print(f"  源③ 资产 mv   {mv_before:.2f}→{mv_after:.2f}（↑{mv_after - mv_before:.2f}）")

    # 源③ 持仓变化（原始查询，含 T+1 冻结）
    raw_after = await _raw_positions(gw)
    mtai_after = raw_after.get(SYMBOL, {})
    vol_after = mtai_after.get("volume", 0)
    can_use_after = mtai_after.get("can_use_volume", 0)
    print(f"  源③ 持仓 茅台 volume {vol_before}→{vol_after}（+{vol_after - vol_before}）can_use={can_use_after}")
    _ok(vol_after - vol_before == QTY, f"持仓 +{QTY}：{vol_after - vol_before == QTY}")
    return result


# === 卖单挂撤段（涨停价卖单不成交；有可卖持仓才测）=======================
async def phase_sell_no_fill(gw, high_limit: float):
    raw = await _raw_positions(gw)
    mtai = raw.get(SYMBOL, {})
    vol = mtai.get("volume", 0)
    can_use = mtai.get("can_use_volume", 0)
    print(f"\n[卖单挂撤段] 茅台原始持仓：volume={vol} can_use_volume={can_use}")
    if can_use < QTY:
        print(f"  可卖 can_use_volume={can_use} < {QTY}（无 T+1 解禁持仓），跳过卖单挂撤。")
        return
    print(f"  有可卖持仓 can_use={can_use}，挂涨停价卖单 {QTY} 股 @涨停价 {high_limit}（不成交）")
    if not _gate(f"挂卖单 {SYMBOL} {QTY}股 @涨停价{high_limit}（不成交预期）"):
        print("  已跳过卖单挂撤段。")
        return

    push_before = _push_count["order"]
    order = OrderRequest(symbol=SYMBOL, qty=QTY, side="sell", price=high_limit)
    result = await gw.submit_order(order)
    print(f"  源① submit → order_id(seq)={result.order_id} state={result.state.name} msg={result.message}")
    if result.state != OrderState.SUBMITTED:
        smsg = _last_status_msg.get("?", "")
        print(f"  [!] 卖单未提交（可能无券/限售）msg={result.message}")
        return result
    await asyncio.sleep(2)
    real_oid = await _resolve_real_oid(gw, result.order_id)
    print(f"  seq={result.order_id} → real_oid={real_oid}")
    _ok(_push_count["order"] > push_before, f"源② 主推：+{_push_count['order'] - push_before} 条")
    rec_push = _read_push_order(gw, real_oid)
    rec_q = await _query_order(gw, real_oid, cancelable_only=True)
    _dump_order("源② gw._orders", rec_push)
    _dump_order("源③ query_orders(cancelable)", rec_q)

    print("  发起撤单...")
    await _cancel_with_retry(gw, result.order_id, real_oid)
    return result


async def _raw_positions(gw) -> dict:
    """原始持仓查询（不过滤 can_use==0，能看到 T+1 当日买入冻结仓）。"""
    if gw._loop is None or gw._trader is None or gw._account is None:
        return {}
    positions = await gw._loop.run_in_executor(
        None, lambda: gw._trader.query_stock_positions(gw._account))
    if not positions:
        return {}
    out = {}
    for p in positions:
        sym = getattr(p, "stock_code", "")
        if not sym:
            continue
        out[sym] = {
            "volume": int(getattr(p, "volume", 0) or 0),
            "can_use_volume": int(getattr(p, "can_use_volume", 0) or 0),
        }
    return out


async def main():
    print("=" * 64)
    print(f"miniQMT 茅台实盘冒烟测试（模拟盘 · {SYMBOL} · {QTY}股 · AUTO={AUTO}）")
    print(f"account={os.getenv('QMT_ACCOUNT_ID')}  userdata={os.getenv('QMT_USERDATA_PATH')}")
    print(f"session={os.getenv('QMT_SESSION_ID', '123456')}")
    print("标尺：三源交叉验证（源①submit返回(seq)/源②gw._orders主推(real)/源③query_orders(real)）")
    print("=" * 64)

    gw = QmtExecutionGateway()
    gw.set_order_update_callback(_on_order_update)

    if _step("步骤1: connect"):
        return
    try:
        await gw.connect()
    except Exception as e:
        print(f"[FAIL] 连接失败：{e}")
        return
    _ok(gw._connected, f"_connected={gw._connected}")
    _ok(not gw.is_locked, f"is_locked={gw.is_locked}")
    _ok(getattr(gw, "_main_push_available", None) is True,
        f"_main_push_available={getattr(gw, '_main_push_available', None)}")
    if not gw._connected:
        print("[FAIL] 连接未成功，终止。")
        return

    if _step("步骤2: query_asset"):
        return
    asset = await gw.query_asset()
    print(f"  资产：{asset}")

    if _step(f"步骤3: get_quote({SYMBOL})"):
        return
    quote = await qmt_market_data.get_quote(SYMBOL)
    print(f"  快照：{quote}")
    if not quote or "last_price" not in quote:
        print("[FAIL] get_quote 无 last_price，终止。")
        await gw.disconnect()
        return
    last = quote.get("last_price")
    hl = quote.get("high_limit")
    ll = quote.get("low_limit")
    ask_price = quote.get("ask_price") or []
    # 成交段挂单价：优先卖一价（ask_price[0]，贴盘口必成交）；缺则回退涨停价。
    # Why 不用涨停价：实测模拟盘 REJECTED 涨停价买单（status_msg 空，疑模拟盘对远离
    # 现价的限价单保守拒），改用卖一价确保撮合成交，验证成交链路本身（成交才是目标）。
    fill_price = ask_price[0] if ask_price else hl
    print(f"  last_price={last}  high_limit={hl}  low_limit={ll}  ask_price={ask_price[:2]}")
    print(f"  成交段挂单价 fill_price={fill_price}（卖一价确保即成）")

    if _step("步骤4-11: 挂撤段（跌停价买单+撤）"):
        return
    if ll:
        if last and last <= ll * 1.001:
            print(f"  [!] 现价≈跌停价（已跌停），跳过挂撤段。")
        else:
            await phase_limit_no_fill(gw, ll)

    if _step(f"步骤12-20: 成交段（卖一价 {fill_price} 买单即成）"):
        return
    if fill_price:
        await phase_limit_fill(gw, fill_price)

    if _step("步骤21-25: 卖单挂撤段（有可卖持仓才测）"):
        return
    if hl:
        await phase_sell_no_fill(gw, hl)

    await gw.disconnect()
    print(f"\n{'=' * 64}\n=== 推送汇总\n{'=' * 64}")
    print(f"  推送计数：{_push_count}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户退出。")
