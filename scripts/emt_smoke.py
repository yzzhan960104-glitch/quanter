# -*- coding: utf-8 -*-
"""EMT 首次真实联调脚本（Phase 1.5 验收）。

前置条件：
  1. .env 已配置 EMT_IP/PORT/USER/PASSWORD（仿真账号 510100014396）
  2. 用 .venv310 跑（vnemttrader.pyd 绑 Python 3.10）

运行：
    .venv310/Scripts/python scripts/emt_smoke.py

铁律（CLAUDE.md 状态机边界）：每步 input() 等待人工确认，绝不批量自动跑真单。
本脚本直连 EMT 网关验证 SDK 连通性（login/资产/持仓/真单/撤单）。

[!] 步骤 5 真单直连网关、不经 risk_shield 挡板——仅限仿真盘小额验证连通性；
    生产实盘下单务必走 POST /api/v1/trading/submit_order（经 10 关挡板）。
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

from trading.emt_gateway import EmtExecutionGateway
from trading.execution_gateway import OrderRequest
from trading.order_state import OrderState


def _step(title: str) -> bool:
    """打印步骤标题，等待回车。返回 True 表示用户要求退出。"""
    print(f"\n{'=' * 60}\n=== {title}\n{'=' * 60}")
    return input("回车继续，q 退出：").strip().lower() == "q"


async def main():
    print("EMT 联调脚本启动。user=", os.getenv("EMT_USER"),
          "ip=", os.getenv("EMT_IP"), "port=", os.getenv("EMT_PORT"))
    print("[!] 本脚本直连 EMT 网关，步骤 5 真单不经风控挡板，仅限仿真盘小额验证。")

    gw = EmtExecutionGateway()

    # --- 步骤 1：login ---
    if _step("步骤 1: login（期望 session!=0, connected=True）"):
        return
    try:
        await gw.connect()
    except (ConnectionError, RuntimeError) as e:
        print(f"[FAIL] 登录失败：{e}")
        print("请核对 .env 的 EMT_IP/PORT/USER/PASSWORD，及仿真账号有效期。")
        return
    print(f"结果：session={gw._session}, connected={gw._connected}, locked={gw.is_locked}")
    if not gw._connected:
        print("[FAIL] 登录未成功，终止。")
        return

    # --- 步骤 2：queryAsset ---
    if _step("步骤 2: queryAsset（期望返资产 dict）"):
        return
    try:
        asset = await gw._fetch_asset()
        print(f"资产：{asset}")
    except Exception as e:
        print(f"[WARN] queryAsset 异常：{e}")

    # --- 步骤 3：queryPosition ---
    if _step("步骤 3: queryPosition（期望返持仓 dict，空也 OK）"):
        return
    try:
        positions = await gw._fetch_broker_positions()
        print(f"持仓（{len(positions)} 只）：{positions}")
    except Exception as e:
        print(f"[WARN] queryPosition 异常：{e}")

    # --- 步骤 4：dry_run 演示（不真下单）---
    if _step("步骤 4: dry_run 演示（白名单内标的，仅打印意图，不调网关）"):
        return
    symbol = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH").split(",")[0].strip()
    print(f"[DRY_RUN] 模拟下单 {symbol} 100 股（不调网关，仅打印意图）")
    print("（dry_run 的端到端验证见 HTTP 层 test_trading_api.py）")

    # --- 步骤 5：真最小限价单（需显式 YES + 用户填价）---
    ans = input(f"\n步骤 5: 真实最小限价单（仿真盘，{symbol} 100 股，直连网关不经挡板）。\n"
                f"输入 YES 继续，其他跳过：").strip()
    if ans != "YES":
        print("已跳过真单步骤。")
        await gw.disconnect()
        return
    price_str = input(f"请输入 {symbol} 的限价（当前价附近，如 5.0）：").strip()
    try:
        price = float(price_str)
    except ValueError:
        print(f"[FAIL] 非法价格：{price_str}，跳过真单。")
        await gw.disconnect()
        return
    print(f"[!] 发起真实限价单 {symbol} 100 股 @ {price}（仿真盘）...")
    order = OrderRequest(symbol=symbol, qty=100, side="buy", price=price)
    result = await gw.submit_order(order)
    print(f"下单结果：order_emt_id={result.order_id}, state={result.state.name}, msg={result.message}")
    if result.state == OrderState.SUBMITTED:
        print("等待 2s 供 onOrderEvent 回调确认柜台受理...")
        await asyncio.sleep(2)
        cancel_res = await gw.cancel_order(result.order_id)
        print(f"撤单结果：state={cancel_res.state.name}, msg={cancel_res.message}")
        if cancel_res.state != OrderState.CANCELLED:
            print("（撤单未确认——可能已成交或柜台拒绝，请到 EMT 客户端核对）")

    await gw.disconnect()
    print("\n联调完成。请核对 logs/live_trades.csv 与 EMT 客户端委托/成交记录。")


if __name__ == "__main__":
    asyncio.run(main())
