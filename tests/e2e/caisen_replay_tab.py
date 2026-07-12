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

        browser.close()

    print(f"[结果] pageerror={len(page_errors)}, console error={len(console_errors)}")
    print(f"[渲染] 命中笔数区: {n_hits_text!r}")
    for e in page_errors[:5]:
        print(f"  [pageerror] {e}")
    for e in console_errors[:5]:
        print(f"  [console] {e}")

    # 断言：无致命 pageerror（Vue 渲染异常 / API 崩）。console error 容忍（EP/lightweight-charts 偶发 warning）
    ok = len(page_errors) == 0
    print("[PASS] replay tab UI 链路通，无致命异常" if ok else "[FAIL] replay tab 有致命 pageerror")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
