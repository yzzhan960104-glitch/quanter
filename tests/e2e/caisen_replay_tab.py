# -*- coding: utf-8 -*-
"""replay tab E2E：验证回测 UI 链路（参数表单 + 年化曲线 + 买卖流水表）真实可用。

物理定位（流程改进·默认 E2E）：UI 改动后浏览器实测是「页面真实可用性」金标准，应与
run_checks 同为开发完默认动作。本脚本验证 task25（replay tab 重写）端到端可用：
goto /caisen → 切「历史回放」tab → 填 universe → 点「运行回放」→ 等回放完成 →
截图（曲线/流水区）→ 断言无致命 pageerror。

断言（核心）：
  1. replay tab 能切换 + 参数表单（策略参数折叠面板）渲染；
  2. 填 universe + 点运行 → 回放完成 → 「买卖流水」section 出现（链路通）；
  3. 无致命 pageerror（Vue 渲染异常 / API 崩）。
默认 cfg 可能 0 命中（曲线/流水空，显示「无数据」提示）——这是策略调参问题，不是 UI bug；
本 E2E 验证 UI 链路可用，不验证命中数。

运行（with_server 起后端 + 前端）：
  VENV=.venv310/Scripts/python.exe
  QUANTER_API_TOKEN=e2e-token python "$WITH_SERVER" \
    --server "$VENV -m uvicorn server.main:app --port 8000" --port 8000 \
    --server "npm --prefix web run dev" --port 5173 --timeout 120 \
    -- "$VENV" tests/e2e/caisen_replay_tab.py
"""
import sys

URL = "http://localhost:5173/caisen"
# 前 10 只 A 股（小池快速回放，~8s）；默认 cfg 多半 0 命中，但验证 UI 链路（非命中数）
UNIVERSE = ",".join([
    "000001.SZ", "000002.SZ", "000063.SZ", "000066.SZ", "000100.SZ",
    "000157.SZ", "000333.SZ", "000338.SZ", "000402.SZ", "000425.SZ",
])


def main() -> int:
    from playwright.sync_api import sync_playwright

    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # 1) 加载 /caisen
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # 2) 切「历史回放」tab
        page.locator(".el-tabs__item").filter(has_text="历史回放").click()
        page.wait_for_timeout(600)

        # 3) 验证策略参数折叠面板渲染（task25 核心组件之一）
        cfg_panel = page.locator("text=策略参数")
        print(f"[check] 策略参数面板存在: {cfg_panel.count() > 0}")

        # 4) 填 universe（replay tab 的标的池输入框）
        page.locator(".replay-area input.el-input__inner").first.fill(UNIVERSE)

        # 5) 点「运行回放」
        page.get_by_role("button", name="运行回放").click()

        # 6) 等回放完成——「买卖流水」section 出现（回放 ~8s + 装配，给足 120s）
        page.wait_for_selector("text=买卖流水", timeout=120000)
        page.wait_for_timeout(1500)  # 等表格/曲线渲染稳定

        # 7) 截图（曲线/流水区，留证）
        page.screenshot(path="tests/e2e/_caisen_replay_tab.png", full_page=True)

        # 8) 读命中笔数（验证回放结果渲染）
        n_hits_text = ""
        hit_loc = page.locator("text=命中笔数").locator("..")
        if hit_loc.count():
            n_hits_text = hit_loc.inner_text()

        # 9) 历史回测记录（方案 A）：默认 save=true → 本次回放应已落盘进历史面板。
        #    展开「历史回测记录」折叠面板（replay-area 内首个 collapse header），等表格渲染，
        #    断言至少 1 条记录（落盘不依赖命中数，0 命中也存）。
        #    行计数 scope 到 .el-table__body-wrapper：fixed="right" 操作列会让 EP 渲染
        #    主表 + 固定右表两份 .el-table__row，裸计数会翻倍（实测 1 行被数成 2）。
        page.locator(".replay-area .el-collapse-item__header").first.click()
        page.wait_for_timeout(800)
        ROWS = ".replay-runs-table .el-table__body-wrapper .el-table__row"
        rows_before = page.locator(ROWS).count()
        print(f"[check] 历史记录条数（回放后）: {rows_before}")
        history_recorded = rows_before >= 1

        # 10) 删除链路：点行内「删除」→ ElMessageBox 确认 → 记录消失。
        history_deleted = True
        if rows_before >= 1:
            page.locator(ROWS).first.get_by_role("button", name="删除").click()
            page.wait_for_selector(".el-message-box", timeout=5000)
            page.wait_for_timeout(300)   # 等 dialog 入场动画完成（避免点击落在遮罩上）
            # 诊断截图：dialog 应打开（排查确认键选择器命中）
            page.screenshot(path="tests/e2e/_caisen_replay_delete_dialog.png", full_page=True)
            # 确认键：Vue 显式 confirmButtonType='danger' → .el-button--danger；
            # scope 到 .el-message-box 与行内 danger「删除」键不混淆（行键在表格内）。
            page.locator(".el-message-box .el-button--danger").click()
            # 轮询等行数下降（比固定 sleep 稳：覆盖 DELETE 请求 + loadReplayRuns 刷新时差）
            try:
                page.wait_for_function(
                    "(n) => document.querySelectorAll("
                    "'.replay-runs-table .el-table__body-wrapper .el-table__row').length <= n",
                    arg=rows_before - 1, timeout=10000,
                )
            except Exception:
                pass   # 超时则照下面实 count 断言
            rows_after = page.locator(ROWS).count()
            print(f"[check] 删除后历史记录条数: {rows_after}（期望 {rows_before - 1}）")
            history_deleted = rows_after == rows_before - 1

        # 11) 截图（历史面板展开态，留证）
        page.screenshot(path="tests/e2e/_caisen_replay_history.png", full_page=True)

        browser.close()

    print(f"[结果] pageerror={len(page_errors)}, console error={len(console_errors)}")
    print(f"[渲染] 命中笔数区: {n_hits_text!r}")
    print(f"[历史] 落盘记录出现={history_recorded}, 删除生效={history_deleted}")
    for e in page_errors[:5]:
        print(f"  [pageerror] {e}")
    for e in console_errors[:5]:
        print(f"  [console] {e}")

    # 断言：无致命 pageerror + 历史落盘 + 删除生效（方案 A 核心契约）。
    ok = len(page_errors) == 0 and history_recorded and history_deleted
    print("[PASS] replay tab UI 链路通 + 历史落盘/删除可用" if ok else "[FAIL] 链路异常或历史/删除未生效")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
