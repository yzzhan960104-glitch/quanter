# -*- coding: utf-8 -*-
"""退役守护 E2E：断言 /caisen 老「历史回放」tab 已下线（Spec 2 Task 8）。

物理定位（流程改进·默认 E2E）：/lab（T6）已接管全部回测能力，/caisen 老「历史回放」
tab（走同步 runReplay API）在 T8 被下线。本脚本改为「退役回归守护」——
goto /caisen → 断言**无**「历史回放」tab（Element Plus .el-tabs__item）→ 断言页面无致命
pageerror → 截图留证。若未来有人误改把回放 tab 加回来（或迁移后 .el-tabs__item 文案漂移），
本 E2E 会立即 RED。

注意：本守护不再跑回放链路（回放能力由 /lab 的 param_lab_smoke.py 覆盖），仅守 /caisen 的
「回放 tab 已删除」契约。扫描/审核/激活链路的可用性由后端 pytest + 该页无 pageerror 间接保证。

运行（with_server 起后端 + 前端）：
  VENV=.venv310/Scripts/python.exe
  QUANTER_API_TOKEN=e2e-token python "$WITH_SERVER" \
    --server "$VENV -m uvicorn server.main:app --port 8000" --port 8000 \
    --server "npm --prefix web run dev" --port 5173 --timeout 120 \
    -- "$VENV" tests/e2e/caisen_replay_tab.py
"""
import sys

URL = "http://localhost:5173/caisen"
# 历史 UNIVERSE 常量保留（回放 tab 退役后不再实际使用，保留以便将来若需复原回放实测时直接可用）。
UNIVERSE = ",".join([
    "000001.SZ", "000002.SZ", "000063.SZ", "000066.SZ", "000100.SZ",
    "000157.SZ", "000333.SZ", "000338.SZ", "000402.SZ", "000425.SZ",
])


def main() -> int:
    from playwright.sync_api import sync_playwright

    page_errors: list[str] = []
    console_errors: list[str] = []

    # tab_removed 在 with 块内赋值、块外读取——Python 的 with 语句不引入新作用域，
    # 故此处先声明占位，确保下方断言段在任何路径下变量都有定义（防御性显式初始化）。
    tab_removed = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # 1) 加载 /caisen（退役后应为纯扫描/审核/激活页，无回放 tab）
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # 2) 退役守护核心断言：「历史回放」tab 已从 .el-tabs__item 列表中消失。
        #    T8 删了 el-tabs 结构（审核区铺平），页面不再有任何 .el-tabs__item——
        #    count()==0 即通过。若有人误把回放 tab 加回来，此断言立即 RED。
        replay_tab = page.locator(".el-tabs__item").filter(has_text="历史回放")
        tab_removed = replay_tab.count() == 0
        print(f"[check] 历史回放 tab 已移除: {tab_removed}")

        # 3) 截图留证（审核区铺平后的 /caisen 全貌）
        page.screenshot(path="tests/e2e/_caisen_replay_tab.png", full_page=True)

        browser.close()

    print(f"[结果] pageerror={len(page_errors)}, console error={len(console_errors)}")
    for e in page_errors[:5]:
        print(f"  [pageerror] {e}")
    for e in console_errors[:5]:
        print(f"  [console] {e}")

    # 断言：无致命 pageerror + 回放 tab 已移除（Spec 2 Task 8 退役契约）。
    ok = len(page_errors) == 0 and tab_removed
    print("[PASS] /caisen 回放 tab 已下线、扫描链路正常" if ok else "[FAIL]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
