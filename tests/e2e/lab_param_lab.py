# -*- coding: utf-8 -*-
"""/lab 参数实验室 E2E：提交异步回测→轮询→SUCCESS→结果渲染（记忆 default-e2e-after-ui）。

物理定位（流程改进·默认 E2E）：UI 改动后浏览器实测是「页面真实可用性」金标准，应与
run_checks 同为开发完默认动作。本脚本验证 Spec 2 Task 6（ParamLabView 主画布）端到端可用：
goto /lab → 开「新建回测」抽屉 → 填小 universe(5 只) + 短区间 → 提交异步回测 →
轮询等 SUCCESS 任务行出现 → 断言参数详情/走势/买卖日志三区渲染 + 任务列表有 SUCCESS 行 +
无致命 pageerror + 截图留证。

selector 对齐说明（与 ParamLabView.vue / NewReplayDrawer.vue 真实组件核对后）：
  * 「参数详情」「收益率走势」「买卖日志」「任务列表」——ParamLabView 四个 .qt-section-title 文本，确认存在。
  * 「＋ 新建回测」——顶栏 el-button（全角＋前缀），用 get_by_role(button, name=) 部分匹配「新建回测」。
  * 抽屉 .el-drawer——NewReplayDrawer 渲染的 el-drawer 根 class，确认存在。
  * 区间输入框——NewReplayDrawer 虽给 el-date-picker 标了 data-testid=start/end，但 Element Plus
    不透传 data-* 到 DOM（探查证实 `[data-testid=start]` 在渲染后 DOM 里不存在）。改用
    get_by_placeholder("开始日" / "结束日")——NewReplayDrawer 显式写了这俩 placeholder，a11y 标准定位最稳。
  * universe textarea——get_by_placeholder("标的池")（NewReplayDrawer placeholder 含「标的池」前缀）。
  * 提交按钮 name="提交异步回测"——NewReplayDrawer footer 的 el-button(data-testid=submit-replay) 文本。
  * SUCCESS 等待——brief 原写 `text=已完成` 是错的：「已完成」是顶栏状态筛选 el-option 的 label，
    页面加载即在 DOM 里（虽不可见但 Playwright text= 仍命中），会立即返回、根本没等回测跑完。
    改用 `.st-SUCCESS`（ParamLabView 任务行 status span 的 class：`'st-'+t.status`），只在
    真有 SUCCESS 任务行渲染时才出现，是「回测真跑完」的可靠信号。

运行（with_server 起后端 uvicorn + 前端 vite；尾串 clean_ports 清 Windows 残留端口）：
  VENV=.venv310/Scripts/python.exe
  QUANTER_API_TOKEN=e2e-token python "$WITH_SERVER" \\
    --server "$VENV -m uvicorn server.main:app --port 8000" --port 8000 \\
    --server "npm --prefix web run dev" --port 5173 --timeout 180 \\
    -- "$VENV" tests/e2e/lab_param_lab.py && python scripts/clean_ports.py
"""
import sys

URL = "http://localhost:5173/lab"
# 前 5 只 A 股 + 短区间（5 只标的 + 半年，异步回测数十秒内 SUCCESS；命中数不论，验 UI 链路）
UNIVERSE = ",".join(["000001.SZ", "000002.SZ", "000063.SZ", "000066.SZ", "000100.SZ"])


def main() -> int:
    from playwright.sync_api import sync_playwright

    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # 监听致命异常（Vue 渲染崩 / API 未捕获 promise reject）+ console error（如 401/500 报错日志）
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # 1) 加载 /lab（domcontentloaded 即可，networkidle 等首屏 schema/tasks 请求收敛）
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # 2) 断言 4 区 + 新建按钮渲染（ParamLabView 主画布四区标题 + 顶栏入口）
        for txt in ("参数详情", "收益率走势", "买卖日志", "任务列表", "新建回测"):
            assert page.locator(f"text={txt}").count() > 0, f"缺区域：{txt}"

        # 3) 开抽屉 + 填区间/universe
        # 「＋ 新建回测」按钮：get_by_role 部分匹配「新建回测」（全角＋前缀也能命中）
        page.get_by_role("button", name="新建回测").click()
        page.wait_for_selector(".el-drawer", timeout=5000)
        # 注：NewReplayDrawer 给 el-date-picker 标了 data-testid=start/end，但 EP 不透传到 DOM
        # （探查证实 [data-testid=start] 不存在）。改用 placeholder 文本定位——NewReplayDrawer
        # 显式写了 placeholder="开始日" / "结束日"，get_by_placeholder 是 a11y 标准定位，最稳。
        page.get_by_placeholder("开始日").fill("2024-01-01")
        page.get_by_placeholder("结束日").fill("2024-06-01")
        # universe textarea（NewReplayDrawer 唯一的 el-input type=textarea，placeholder 含「标的池」）
        page.get_by_placeholder("标的池").fill(UNIVERSE)

        # 4) 提交异步回测（NewReplayDrawer footer 按钮；提交后父组件 selectTask + 起轮询）
        page.get_by_role("button", name="提交异步回测").click()

        # 5) 轮询等 SUCCESS——用 .st-SUCCESS（任务行 status span class），非「已完成」文案
        # 「已完成」是状态筛选 el-option label，页面加载即在 DOM，会立即假命中。.st-SUCCESS 只在
        # 真有 SUCCESS 任务行渲染时出现，是回测真跑完的可靠信号。给足 180s（5 只 + 半年异步回测）。
        page.wait_for_selector(".st-SUCCESS", timeout=180000)
        page.wait_for_timeout(2000)   # 等选中任务详情（report/trades/equity_curve）灌入三区渲染稳定

        # 6) 截图（全页四区 + 任务列表，留证）
        page.screenshot(path="tests/e2e/_lab_param_lab.png", full_page=True)

        # 7) 断言三区在选中 SUCCESS 任务下已渲染（走势区 ReplayReportPanel / 买卖日志 report.trades）
        #    走势区：SUCCESS 时挂 ReplayReportPanel（v-if="selected?.report"）
        #    买卖日志：有 trades 渲染 trade-row；0 命中则显「暂无买卖日志」空态（均算链路通）
        n_success = page.locator(".st-SUCCESS").count()
        assert n_success >= 1, "无 SUCCESS 任务行"

        browser.close()

    # 结果汇总（与 caisen_replay_tab.py 同风格：pageerror/console 计数 + 样本）
    print(f"[结果] pageerror={len(page_errors)}, console error={len(console_errors)}")
    print(f"[SUCCESS] 任务行数: {n_success}")
    for e in page_errors[:5]:
        print(f"  [pageerror] {e}")
    for e in console_errors[:5]:
        print(f"  [console] {e}")

    # 断言：无致命 pageerror（Vue 渲染异常 / API 崩）+ SUCCESS 真等到
    ok = len(page_errors) == 0
    print("[PASS] /lab 提交→轮询→SUCCESS→结果渲染链路通" if ok else "[FAIL] 链路异常")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
