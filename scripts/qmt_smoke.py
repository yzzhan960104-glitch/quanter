# -*- coding: utf-8 -*-
"""QMT 首次真实联调脚本（Phase 1 验收）。

前置条件：
  1. 模拟盘 XtItClient.exe 已启动并登录账号 62138335
  2. userdata_mini 目录已生成（D:\\国金QMT交易端模拟\\userdata_mini）
  3. .env 已配置 QMT_USERDATA_PATH / QMT_ACCOUNT_ID

运行：python scripts/qmt_smoke.py

铁律（CLAUDE.md 状态机边界）：每步 input() 等待人工确认，绝不批量自动跑真单。
本脚本直连网关验证 SDK 连通性（connect/资产/持仓/真单/撤单），dry_run 与风控
挡板的端到端验证由 HTTP 层（test_trading_api.py）覆盖。

[!] 步骤 5 真单直连网关、不经 risk_shield 挡板——仅限模拟盘小额验证连通性；
    生产实盘下单务必走 POST /api/v1/trading/submit_order（经 10 关挡板）。
"""
import asyncio
import os
import sys

# 把项目根加入 sys.path（脚本独立运行需要）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# 触发 .env 加载（python-dotenv 若有；否则依赖外部已 export）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from trading.qmt_gateway import QmtExecutionGateway
from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：垫片已删，直指 compute.types 真身
from trading.types.order_state import OrderState  # Layer2 follow-up #4c：改指 types 真身


def _step(title: str) -> bool:
    """打印步骤标题，等待回车。返回 True 表示用户要求退出。"""
    print(f"\n{'=' * 60}\n=== {title}\n{'=' * 60}")
    return input("回车继续，q 退出：").strip().lower() == "q"


async def main():
    print("QMT 联调脚本启动。account=", os.getenv("QMT_ACCOUNT_ID"))
    print("[!] 本脚本直连网关，步骤 5 真单不经风控挡板，仅限模拟盘小额验证。")

    gw = QmtExecutionGateway()

    # --- 步骤 1：connect ---
    if _step("步骤 1: connect（期望 _connected=True, _lock_down=False）"):
        return
    try:
        await gw.connect()
    except ConnectionError as e:
        print(f"[FAIL] 连接失败：{e}")
        print("请确认 XtItClient.exe 已启动登录，且 QMT_USERDATA_PATH 路径正确。")
        return
    print(f"结果：_connected={gw._connected}, is_locked={gw.is_locked}")
    if not gw._connected:
        print("[FAIL] 连接未成功，终止。")
        return

    # --- 步骤 2：query_asset ---
    if _step("步骤 2: query_asset（期望返回 XtAsset 现金/总资产）"):
        return
    loop = asyncio.get_running_loop()
    asset = await loop.run_in_executor(
        None, lambda: gw._trader.query_stock_asset(gw._account)
    )
    print(f"资产：{asset}")

    # --- 步骤 3：positions ---
    if _step("步骤 3: query_positions（期望返回持仓 list，空也 OK）"):
        return
    positions = await gw._fetch_broker_positions()
    print(f"持仓（{len(positions)} 只）：{positions}")

    # --- 步骤 4：dry_run 演示（不真下单）---
    if _step("步骤 4: dry_run 演示（白名单内标的，仅打印意图，不调网关）"):
        return
    symbol = os.getenv("QMT_SYMBOL_WHITELIST", "510300.SH").split(",")[0].strip()
    print(f"[DRY_RUN] 模拟下单 {symbol} 100 股 @5.0（不调网关，仅打印意图）")
    print("（dry_run 的端到端验证见 HTTP 层 test_trading_api.py::test_submit_order_dry_run）")

    # --- 步骤 5：真最小限价单（需显式 YES 确认）---
    ans = input("\n步骤 5: 真实最小限价单（模拟盘，100 股，直连网关不经挡板）。"
                "输入 YES 继续，其他跳过：").strip()
    if ans != "YES":
        print("已跳过真单步骤。")
        await gw.disconnect()
        return
    print("[!] 发起真实限价单 100 股（模拟盘）...")
    order = OrderRequest(symbol=symbol, qty=100, side="buy", price=5.0)
    result = await gw.submit_order(order)
    print(f"下单结果：order_id={result.order_id}, state={result.state.name}, msg={result.message}")
    if result.state == OrderState.SUBMITTED:
        # 等待 on_order_stock_async_response 回调建立 seq→real 映射后撤单
        print("等待 2s 供 async_response 回调建立 seq→real 映射...")
        await asyncio.sleep(2)
        cancel_res = await gw.cancel_order(result.order_id)
        print(f"撤单结果：state={cancel_res.state.name}, msg={cancel_res.message}")
        if cancel_res.state != OrderState.CANCELLED:
            print("（撤单未确认——可能是映射未就绪或已成交，请到 XtItClient 客户端核对）")

    await gw.disconnect()
    print("\n联调完成。请核对 logs/live_trades.csv 与 XtItClient 客户端委托/成交记录。")


if __name__ == "__main__":
    asyncio.run(main())
