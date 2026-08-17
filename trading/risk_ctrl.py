# -*- coding: utf-8 -*-
"""人工风控双值 CLI（ADR-16 · 2026-08-17）。

用法：
    python -m trading.risk_ctrl                     # 查看当前生效值 + 存储真值
    python -m trading.risk_ctrl block on            # 拦截一切新买单（增量；卖出/退出不拦）
    python -m trading.risk_ctrl block off           # 解除拦截
    python -m trading.risk_ctrl position 0.8        # 总仓位上限 80%
    python -m trading.risk_ctrl position 1.0        # 解除总仓位限制

物理意图：regime 指标闸移除后的日常人工风控通道（与 REST PUT /risk/control 同一
state_store.write_risk_control 单源）。独立进程直写 sqlite（WAL + busy_timeout 兜底，
DG-G6「仅事件循环写」红线不适用于外部运维进程的单键 UPSERT）。
"""
from __future__ import annotations

import sys


def _main(argv: list[str]) -> int:
    from trading.state_store import read_risk_control, resolve_risk_control

    if not argv:
        resolved = resolve_risk_control()
        raw = read_risk_control()
        state = "拦截增量下单" if resolved["block"] else "放行增量下单"
        pos = "不限制" if resolved["max_pos"] >= 1.0 else f"{resolved['max_pos']:.0%}"
        print(f"block_new_orders : {raw['block_new_orders']!r:8} → {state}")
        print(f"max_total_position: {raw['max_total_position']!r:8} → 总仓位上限 {pos}")
        if resolved["degraded"]:
            print("⚠ degraded=True：存在非法存储值或读取异常，按 fail-closed/默认口径执行")
        return 0

    cmd = argv[0]
    if cmd == "block":
        if len(argv) != 2 or argv[1] not in ("on", "off"):
            print("用法：python -m trading.risk_ctrl block on|off", file=sys.stderr)
            return 2
        from trading.state_store import write_risk_control
        result = write_risk_control(block_new_orders=(argv[1] == "on"))
        print(f"已设 block_new_orders={argv[1]} → "
              f"{'拦截增量下单' if result['block'] else '放行增量下单'}")
        return 0

    if cmd == "position":
        if len(argv) != 2:
            print("用法：python -m trading.risk_ctrl position <0.0~1.0>", file=sys.stderr)
            return 2
        try:
            value = float(argv[1])
        except ValueError:
            print(f"position={argv[1]!r} 非数字", file=sys.stderr)
            return 2
        from trading.state_store import write_risk_control
        try:
            result = write_risk_control(max_total_position=value)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        pos_note = "不限制" if result["max_pos"] >= 1.0 else f"{result['max_pos']:.0%}"
        print(f"已设 max_total_position={result['max_pos']} （{pos_note}）")
        return 0

    print(f"未知命令 {cmd!r}（合法：block / position / 空=查看）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
