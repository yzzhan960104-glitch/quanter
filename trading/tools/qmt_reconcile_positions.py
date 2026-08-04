# -*- coding: utf-8 -*-
"""QMT 真实持仓交叉校验（清账本脏数据前的安全 gate · 纯只读零副作用）。

物理意图：
    删除 logs/trading_state.db 中「特锐德 300001.SZ 100 / 神州泰岳 300002.SZ 100.5」
    两条疑似测试污染数据前，必须连 miniQMT 拉券商真实持仓做交叉校验，证明这两条
    **确实是测试脏数据、而非实盘真仓**——否则误删真实仓位 = 敞口错算（CLAUDE.md
    「删除前先核对目标，矛盾就报告」红线）。

三源交叉（关键：用源 C 判定，绝不能用源 B 单独判定）：
  - 源 A 本地账本：``position_book.get_local_positions()`` —— 被校验对象（争议数据）
  - 源 B 券商可操作：``gw._fetch_broker_positions()`` —— **过滤** can_use_volume==0
    （broker/qmt.py:425），T+1 当日买入会被滤掉，单独作判据会误判「实盘无此仓」→ 误删
  - 源 C 券商原始：``query_stock_positions`` 不过滤（复用 qmt_live_smoke_moutai._raw_positions
    同款实现），能看到 T+1 当日买入冻结仓 —— **权威判定源**

GO/NO-GO（以源 C 为准）：
  - GO（放行清理）：源 C 不含 300001/300002（或全量空仓），即实盘真无此两仓 → 退出码 0
  - NO-GO（停·不动账本）：源 C 含任一争议票；或连接失败 / 拉持仓异常 —— 一律按
    「无法证伪」保守处理，退出码 1
  - 边界：query_stock_positions 返 None（查询失败与当日空仓不可区分，broker/qmt.py:418）
    被 _raw_positions 统一当空 → 源 C 空 = GO，但输出显式标注 None 语义歧义

旁证：
  - QMT 原始 volume 是 int（_raw_positions 里 int() 转），真实持仓不会有 0.5 股；
    若本地 qty 带小数而 QMT 无此仓，是测试污染硬旁证。
  - query_orders 扫 order_id∈{123,456} 诡异单号（成交后未必查得到，有则铁证）。

铁律：纯只读、零副作用——不 submit_order / cancel_order / 写 position_book。

用法：
    .venv310/Scripts/python.exe trading/tools/qmt_reconcile_positions.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# 切 stdio UTF-8（Windows GBK 控制台编码不了 ✅/❌ 等，与 run_trading_engine.bat 的
# PYTHONUTF8=1 同理，防中文/Unicode 符号乱码）。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 三层 dirname：本脚本 → tools → trading → 项目根（与 qmt_live_smoke_moutai.py 同范式）。
# ⚠️ 两层 dirname 只到 trading/，会被塞 sys.path[0] 遮蔽标准库 calendar（commit 049db6ce
# 根因），必须三层。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_PROJECT_ROOT)  # position_book._DEFAULT_DB 是相对路径 logs/...，须 cwd=项目根
sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

import trading.position_book as position_book
from trading.qmt_gateway import QmtExecutionGateway

# 争议标的（来自 logs/trading_state.db 当前的疑似测试污染行：order_id=123/456）。
DISPUTED = ["300001.SZ", "300002.SZ"]


async def _raw_positions(gw) -> dict:
    """源 C：券商原始持仓（不过滤 can_use，能看到 T+1 当日买入冻结仓）—— 权威判定源。

    复用 qmt_live_smoke_moutai.py:372 的同款实现（茅台实测已验证可用）。
    volume 转 int：QMT 返回 int 股数，真实持仓不会出现 0.5 股（A 股整手约束）。
    未连接时返 {}（与 moutai 同口径，防 None 指针）。
    """
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


def _read_local_book() -> dict:
    """源 A：读本地账本 {symbol: qty}。表不存在/异常 → 当空（只读脚本不建表、无副作用）。"""
    try:
        return position_book.get_local_positions()
    except Exception as e:
        print(f"  [源A] 读本地账本异常（当空处理）：{type(e).__name__}: {e}")
        return {}


async def main() -> int:
    print("=" * 64)
    print("QMT 持仓交叉校验（清账本前安全 gate · 纯只读）")
    print(f"争议标的：{DISPUTED}（本地账本疑似测试污染行 order_id=123/456）")
    print(f"account={os.getenv('QMT_ACCOUNT_ID')}  userdata={os.getenv('QMT_USERDATA_PATH')}")
    print("=" * 64)

    # === 源 A：本地账本（无需连 QMT）===
    local = _read_local_book()
    print(f"\n[源A 本地账本] 全量 {len(local)} 只")
    for sym in DISPUTED:
        print(f"  - {sym}: qty={local.get(sym, '<无>')}")

    # === 连 QMT ===
    print("\n[连接] QmtExecutionGateway.connect()...")
    gw = QmtExecutionGateway()
    try:
        await gw.connect()
    except Exception as e:
        print(f"\n[NO-GO] 连接失败：{type(e).__name__}: {e}")
        print("       → 无法证伪实盘持仓，按风控红线【不动账本】。")
        return 1
    print(f"  _connected={gw._connected} is_locked={gw.is_locked} "
          f"main_push={getattr(gw, '_main_push_available', None)}")

    # 拉取源 B/C；任一异常 → NO-GO（finally 确保 disconnect，不留连接泄漏）
    try:
        # 源 B：券商可操作持仓（过滤 can_use==0，T+1 当日买入看不到）
        broker_oper = await gw._fetch_broker_positions()
        # 源 C：券商原始持仓（不过滤·权威判定源）
        broker_raw = await _raw_positions(gw)
        # 旁证：同连接查委托扫 order_id∈{123,456}（不重连——miniQMT 同进程同 session
        # 重连会返 -1 占用冲突；成交后未必查得到，有则铁证）。异常忽略，不阻断主判定。
        try:
            orders = await gw.query_orders(cancelable_only=False)
            weird = [o for o in orders if str(o.get("order_id")) in ("123", "456")]
            print(f"\n[旁证] query_orders 共 {len(orders)} 笔，order_id∈{{123,456}} 命中 "
                  f"{len(weird)} 笔" + (f"：{[w.get('order_id') for w in weird]}" if weird else ""))
        except Exception as e:
            print(f"\n[旁证] 查委托异常（忽略，不阻断判定）：{type(e).__name__}: {e}")
    except Exception as e:
        print(f"\n[NO-GO] 拉持仓异常：{type(e).__name__}: {e}")
        print("       → 无法证伪实盘持仓，按风控红线【不动账本】。")
        return 1
    finally:
        await gw.disconnect()

    # 以下纯内存对照（已 disconnect），无副作用
    print(f"\n[源B 券商可操作 _fetch_broker_positions] 全量 {len(broker_oper)} 只（已过滤 can_use==0）")
    for sym in DISPUTED:
        rec = broker_oper.get(sym)
        print(f"  - {sym}: {rec if rec else '<无>'}")

    print(f"\n[源C 券商原始 query_stock_positions] 全量 {len(broker_raw)} 只（不过滤·权威）")
    for sym in DISPUTED:
        rec = broker_raw.get(sym)
        print(f"  - {sym}: {rec if rec else '<无>'}")
    if broker_raw:
        print("  全量明细：")
        for sym, rec in sorted(broker_raw.items()):
            print(f"    {sym}: volume={rec['volume']} can_use={rec['can_use_volume']}")
    else:
        print("  ⚠️ 源 C 全量空（注意：query_stock_positions 返 None = 查询失败与空仓不可区分）")

    # === GO/NO-GO 判定（以源 C 为准）===
    print("\n" + "=" * 64)
    print("=== 判定（源 C 权威）===")
    hits = [sym for sym in DISPUTED if sym in broker_raw]

    if not hits:
        print(f"[GO] 源 C 不含 {DISPUTED}（实盘真无此两仓）—— 确认测试污染，可放行清理。")
        for sym in DISPUTED:
            q = local.get(sym)
            if q is not None and float(q) != int(q):
                print(f"  旁证：本地 {sym} qty={q} 带小数，QMT 原始 volume 必为 int —— 测试污染硬旁证。")
        return 0
    else:
        print(f"[NO-GO] 源 C 含争议标的 {hits} —— 可能非纯测试污染！")
        for sym in hits:
            print(f"  {sym}: {broker_raw[sym]}")
        print("       → 按风控红线【不动账本】，请人工核 miniQMT 客户端。")
        return 1


if __name__ == "__main__":
    # stdout UTF-8 治理:防 GBK 管道崩 emoji(详见 infra/pyio.py)
    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[中断] 用户退出。")
        sys.exit(130)
