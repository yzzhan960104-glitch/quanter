# -*- coding: utf-8 -*-
"""二期 engine 影子模式端到端冒烟脚本（一次性 · 不真发钉钉）。

物理意图：Task 11 上线前验证 engine.eod_plan 全链路正常——
  signals=[] + atr_map={} + capital=1_000_000
  → trading_plan.save_plan 落盘 logs/trading_plans/plan_<today>.json
  → trading_plan.push_plan_to_dingtalk 推钉钉（本脚本 monkeypatch 防真发 dws）
  → 返回 {date, n_orders:0, mode:dry_run}

Why monkeypatch push_brief（而非 push_plan_to_dingtalk）：
  push_plan_to_dingtalk 内部 import 调 broadcast.push.push_brief（顶层 import，
  同模块符号）；patch broadcast.push.push_brief 后，trading_plan.py 顶层的
  `from broadcast.push import push_brief` 是**符号绑定快照**（模块加载时已确定），
  patch broadcast.push.push_brief **不会**影响 trading_plan 持有的引用。
  故必须 patch trading_plan.push_brief（trading_plan 模块命名空间里的那个名字），
  方能拦住 push_plan_to_dingtalk 的真实调用。

幂等：可重复跑（save_plan 用 json.dump 覆盖写，plan_<today>.json 幂等覆盖）。

用法：
    .venv310/Scripts/python.exe scripts/smoke_trading_engine.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 锁定 cwd 在项目根（trading_plan.TRADE_PLAN_DIR 默认 logs/trading_plans 相对路径，
# 必须从项目根跑，否则 plan 落盘到错位置）。
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

# 把项目根加到 sys.path 头部（否则 scripts/ 下脚本直接跑时 Python 不认项目根包；
# -m python scripts/xxx.py 不适用本脚本场景，用户直接 python scripts/xxx.py 跑）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 切 stdio UTF-8（Windows GBK 控制台默认编码不了 ✅/❌ 等 Unicode 符号，
# 与 run_trading_engine.bat 的 PYTHONUTF8=1 同理，防脚本输出乱码/UnicodeEncodeError）。
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 影子模式硬约束：冒烟脚本绝不切 live，绝不真发钉钉。
os.environ["AUTO_TRADE_MODE"] = "dry_run"
# 计划落盘目录用默认（logs/trading_plans），不重定向到临时目录——便于人工肉眼核对文件。
# 如需隔离测试，可取消下行注释指向临时目录。
# os.environ["TRADE_PLAN_DIR"] = "logs/trading_plans_smoke"


def _smoke() -> int:
    """执行冒烟 · 返进程退出码（0=成功，1=失败）。"""
    # ① monkeypatch trading_plan.push_brief：拦住 push_plan_to_dingtalk 的 dws 出站
    #    （见模块 docstring 解释为何 patch trading_plan 命名空间而非 broadcast.push）。
    import trading.trading_plan as tp

    pushed: list[dict] = []

    def _fake_push(title: str, markdown: str, *, robot_code: str, group_id: str,
                   dry_run: bool = False, timeout: int = 30) -> bool:
        pushed.append({"title": title, "markdown": markdown,
                       "robot_code": robot_code, "group_id": group_id})
        print(f"[smoke] 拦截 push_brief（未真发 dws）：title={title!r}")
        return True

    tp.push_brief = _fake_push  # type: ignore[assignment]

    # ② 触发 eod_plan（空信号 → 空 orders）
    from trading.engine import eod_plan

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"[smoke] 触发 eod_plan(date={today}, signals=[], atr_map={{}}, capital=1e6)")
    result = asyncio.run(
        eod_plan(today, signals=[], atr_map={}, capital=1_000_000.0)
    )
    print(f"[smoke] eod_plan 返回：{result}")

    # ③ 验证：落盘文件存在 + 内容 {date, confirmed:False, orders:[]}
    plan_dir = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    plan_path = plan_dir / f"plan_{today}.json"
    assert plan_path.exists(), f"❌ 计划文件未落盘：{plan_path}"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    print(f"[smoke] 落盘文件 {plan_path}")
    print(f"[smoke] 落盘内容：{json.dumps(payload, ensure_ascii=False)}")

    # 断言链（任一失败即抛 AssertionError → 退出码 1）
    assert payload["date"] == today, f"date 不匹配：{payload['date']} != {today}"
    assert payload["confirmed"] is False, f"confirmed 必须为 False（T-1 待确认）：{payload['confirmed']}"
    assert payload["orders"] == [], f"空信号应产空 orders：{payload['orders']}"
    assert result == {"date": today, "n_orders": 0, "mode": "dry_run"}, \
        f"返回值不符预期：{result}"

    # ④ 验证 push 也被拦（monkeypatch 生效 → 未真发 dws）
    assert len(pushed) == 1, f"push_brief 应被拦 1 次，实际 {len(pushed)} 次"
    assert today in pushed[0]["title"], f"推送 title 应含日期：{pushed[0]['title']}"

    print("[smoke] ✅ 全部断言通过：影子模式落盘 + 推送拦截 + 返回值一致")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_smoke())
    except AssertionError as e:
        # 断言失败：清晰打印错误，退出码 1（不炸堆栈，方便人工看）
        print(f"[smoke] ❌ 断言失败：{e}")
        sys.exit(1)
    except Exception:
        # 其他异常：打印完整堆栈供排查（如 import 失败、文件 IO 权限等）
        import traceback
        print("[smoke] ❌ 冒烟异常：")
        traceback.print_exc()
        sys.exit(2)
