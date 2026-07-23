# -*- coding: utf-8 -*-
"""miniQMT 模拟盘限频实测（Task8 · spec §10 待核实点）。

物理意图
========
``TradingEngine`` 的 stop_loss 巡检周期由 cron ``*/5 9-14``（5min）改为
``IntervalTrigger(seconds=30)``（Task8）。30s 巡检每轮会连调
``gw._fetch_broker_positions()``（query_stock_positions）+ 每持仓标的一次
``gw.get_quote()``（get_quote → xtdata.get_full_tick）。本脚本在模拟盘连接状态下，
**按 30s 节奏连续打 20 轮（约 10 分钟）**，观察是否撞柜台限流
（``too many`` / ``频率`` / 超时激增 / xtdata 报错）。

判定逻辑（spec §10 → ENGINE_STOPLOSS_INTERVAL_SECONDS 定终值）：
- 20 轮全 ok，无报错 → 30s 可用，``ENGINE_STOPLOSS_INTERVAL_SECONDS=30`` 定稿。
- 出现限流 → 上调到 60s，重测，定终值并在 ``.env.example`` 注释实测结论。

运行
====
.venv310/Scripts/python.exe scripts/probe_qmt_ratelimit.py

前置
====
- miniQMT 客户端已启动登录（ userdata_mini 目录已生成）。
- ``.env`` 已配 QMT_USERDATA_PATH / QMT_ACCOUNT_ID（headless 联调脚本同款）。
- 默认取 QMT_SYMBOL_WHITELIST 的标的做 get_quote（无配置回退 510300.SH/510500.SH）。

注：本脚本**严格只读**（connect / positions / get_quote），不发任何 submit/cancel。
"""
import asyncio
import os
import sys
import time
import traceback

# Windows 控制台默认 GBK，中文/emoji UnicodeEncodeError——强制 stdout/stderr 走 utf-8
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

# ----------------------------------------------------------------------------
# 配置：循环轮数 + 每轮间隔（模拟 stop_loss 每 30s 触发）。
# 默认 20 轮 × 30s ≈ 10min；用环境变量可缩短（PROBE_ROUNDS / PROBE_INTERVAL）。
# Grill Me 边界：若研究员只想快速看前几轮是否立即限流，可 PROBE_ROUNDS=3 跑 90s。
# ----------------------------------------------------------------------------
ROUNDS = int(os.getenv("PROBE_ROUNDS", "20"))
INTERVAL = int(os.getenv("PROBE_INTERVAL", "30"))

# get_quote 目标标的：优先 QMT_SYMBOL_WHITELIST，回退两只宽基 ETF
_symbols_env = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH,510500.SH")
PROBE_SYMBOLS = [s.strip() for s in _symbols_env.split(",") if s.strip()] or [
    "510300.SH",
    "510500.SH",
]


async def _probe_round(gw, round_idx: int) -> dict:
    """单轮限频探测：模拟 stop_loss 一次 tick 的双调用（positions + quotes）。

    Returns:
        {round, pos_ok, pos_count, quote_ok, quote_errs, elapsed_s, errors}
    """
    t0 = time.time()
    result = {
        "round": round_idx,
        "pos_ok": False,
        "pos_count": 0,
        "quote_ok": 0,
        "quote_errs": [],
        "elapsed_s": 0.0,
        "errors": [],
    }
    # 1) query_stock_positions（等价 stop_loss_monitor 入口的 gw._fetch_broker_positions）
    try:
        positions = await gw._fetch_broker_positions()
        result["pos_ok"] = True
        result["pos_count"] = len(positions) if positions else 0
    except Exception as exc:
        # 柜台限流通常在此处抛（too many / 频率 / 超时）
        result["errors"].append(f"positions: {type(exc).__name__}: {exc}")

    # 2) 逐标的 get_quote（xtdata.get_full_tick）——最多打 PROBE_SYMBOLS 只，
    #    若持仓非空也补一轮持仓 symbol（模拟真实巡检覆盖持仓）
    symbols_to_probe = list(PROBE_SYMBOLS)
    if result["pos_ok"] and positions:
        # 持仓 symbol 优先（真实巡检语义），保留前几只避免一轮打太多 symbol 本身超频
        for sym in list(positions.keys())[:3]:
            if sym not in symbols_to_probe:
                symbols_to_probe.append(sym)
    for sym in symbols_to_probe:
        try:
            q = await gw.get_quote(sym)
            if q is None:
                result["quote_errs"].append(f"{sym}: get_quote None（标的/行情源缺）")
            else:
                result["quote_ok"] += 1
        except Exception as exc:
            result["quote_errs"].append(f"{sym}: {type(exc).__name__}: {exc}")

    result["elapsed_s"] = round(time.time() - t0, 2)
    return result


async def main() -> int:
    # 延迟 import：让 sys.path / dotenv 先生效，便于报错栈清晰
    from trading.qmt_gateway import QmtExecutionGateway

    print(f"[probe] rounds={ROUNDS} interval={INTERVAL}s symbols={PROBE_SYMBOLS}")
    print("[probe] 构造 QmtExecutionGateway（读 .env QMT_USERDATA_PATH/QMT_ACCOUNT_ID）...")
    try:
        gw = QmtExecutionGateway()
    except Exception:
        print("[probe][FATAL] QmtExecutionGateway 构造失败（缺 QMT_USERDATA_PATH/QMT_ACCOUNT_ID）：")
        traceback.print_exc()
        return 2

    print("[probe] 连接 miniQMT...")
    try:
        await gw.connect()
    except Exception:
        print("[probe][FATAL] gw.connect() 失败——miniQMT 客户端未启动 / userdata 路径错 / 账号未登录：")
        traceback.print_exc()
        return 3

    print("[probe] 连接成功，开始限频探测...\n")
    limit_hit = False
    try:
        for i in range(ROUNDS):
            res = await _probe_round(gw, i)
            flag = "OK" if res["pos_ok"] and not res["errors"] else "ERR"
            # 限流关键字探测（柜台 / xtdata 限频措辞多见）
            blob = " ".join(res["errors"] + res["quote_errs"]).lower()
            if any(kw in blob for kw in ("too many", "频率", "frequent", "limit", "429", "rate")):
                flag = "RATE-LIMIT"
                limit_hit = True
            print(
                f"[{i:02d}] {flag} pos_ok={res['pos_ok']} pos={res['pos_count']} "
                f"quote_ok={res['quote_ok']}/{len(PROBE_SYMBOLS)} "
                f"elapsed={res['elapsed_s']}s"
            )
            if res["errors"]:
                for e in res["errors"]:
                    print(f"     ! {e}")
            if res["quote_errs"]:
                for e in res["quote_errs"][:3]:
                    print(f"     ! quote {e}")
            if i < ROUNDS - 1:
                await asyncio.sleep(INTERVAL)
    finally:
        try:
            await gw.disconnect()
        except Exception:
            pass

    print("\n[probe] 结论：")
    if limit_hit:
        print(
            "  ⚠️ 检测到限流迹象——建议上调 ENGINE_STOPLOSS_INTERVAL_SECONDS=60 重测。"
        )
        return 1
    print(
        f"  ✅ {ROUNDS} 轮（×{INTERVAL}s）未观察限流——30s 可用，"
        "ENGINE_STOPLOSS_INTERVAL_SECONDS=30 可定稿。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
