# -*- coding: utf-8 -*-
"""实验系统 CLI：python -m experiment create|promote|set-weight|archive|rollback|list|report

每个命令操作 experiment/experiments.db，变更写审计。退出码：0 成功 / 非 0 失败。
now 时间戳由调用方传或取当前；测试传固定值保证可复现。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from experiment.models import ExperimentVersion, ExperimentStatus
from experiment.store import _DEFAULT_DB, archive as _archive, create_version, list_versions, promote as _promote, rollback as _rollback, set_weight

_OPERATOR = "cli"

# 未带 experiment_id 的老 order 在归因表中统一归到这个桶，
# 避免把历史回测单子与新实验单子混在一起导致归因失真。
_UNATTRIBUTED = "未归因"


def _now() -> str:
    """当前 ISO 时间戳（CLI 实跑用；测试走 store 层固定 now）。"""
    return datetime.now().isoformat(timespec="seconds")


def _load_all_plans(since: str = None) -> list:
    """读全量交易计划返回 plan dict 列表（C2d 切 DB · SSoT 真相源）。

    Why：experiment report 要按 experiment_id 聚合归因，必须先把全量计划读出来供下游分组。
    设计要点：
    - **数据源（C2d）**：从 state_store.trade_event(SIGNAL).meta 读，**不再扫 plan_*.json**。
      Why DB：plan_*.json 的文件 mtime = T 日盘后写入时间，**非计划日 T+1**；若 since 按
      mtime/文件 date 字段过滤，T 日盘后写入与 T+1 计划生效日差一天，since 永远偏移。
      DB 查 ``substr(trade_id,-10) >= since``（trade_id 后缀 = plan_date，build_trade_id 单点）
      保证 since 锚的是真实计划日。详见 state_store.list_signals_with_meta_by_plan_date_range。
    - **shape 兼容**：DB 返 ``[{symbol, plan_date, **meta}]``，这里按 plan_date 分组包成
      ``[{date: plan_date, orders: [...]}]``（orders[] 每项即 SIGNAL meta，含 experiment_id /
      experiment_weight / order 字段）——下游 report 聚合逻辑（``p["orders"]`` + ``o["order"]["symbol"]``）
      完全不动，纯换数据源。
    - since/until 走 plan_date 字典序（YYYY-MM-DD 字典序 == 时间序，与原 JSON 口径一致）。
    - DB 读失败静默返空列表（防御性，不让 audit 链路炸；下游 groups={} 自然打出空表）。
    """
    # 延迟 import 避免循环（experiment 包可能先于 trading 包加载的场景）。
    try:
        from trading import state_store
    except Exception:
        # trading 包未就绪（如纯实验系统单测场景）：返空，report 出空表，不崩。
        return []
    try:
        rows = state_store.list_signals_with_meta_by_plan_date_range(since=since)
    except Exception:
        # DB 读失败（无 init / 表缺失 / 文件锁）：防御性返空，不阻断 CLI 主流程。
        return []
    # 按 plan_date 分组包成 plan dict 列表（shape 与原 JSON 版本兼容）。
    by_date: dict[str, dict] = {}
    for r in rows:
        pd = r.get("plan_date")
        if not pd:
            continue
        # meta 行即 order 项（含 order/experiment_id/experiment_weight/stop_price 等全字段）。
        by_date.setdefault(pd, {"date": pd, "orders": []})["orders"].append(r)
    # 按 date 字典序返（与原 sorted(glob) 同口径，保证 report 输出可复现）。
    return [by_date[k] for k in sorted(by_date.keys())]


def _report(args) -> int:
    """按 experiment_id 聚合 trading_plan 中的 orders：订单数 / 权重 / 涉及标的数。

    Why：A/B 实验跑完后，研究员要回答"prod vs candidate 各自真正下了多少单、
    占多少权重、覆盖多少标的"，必须把每日落盘 plan 里的 orders 按 experiment_id
    切片汇总，这是归因审计闭环的最后一公里。
    设计要点：
    - 同一 experiment_id 的 weight 在 Task 6 透传时保证 plan 内一致，这里取最后一笔
      覆盖即可（不同 plan 间 weight 会被新 plan 的事件 promote/set-weight 覆盖更新）。
    - 无 experiment_id 的老 order 统一进「未归因」桶，与实验单隔离，审计不混淆。
    - symbols 用 set 去重，看真实触达的标的广度而非下单次数。
    """
    plans = _load_all_plans(args.since)
    groups = {}
    for p in plans:
        for o in p.get("orders", []):
            # 缺 experiment_id 视为历史单：归未归因桶，不崩
            eid = o.get("experiment_id") or _UNATTRIBUTED
            g = groups.setdefault(eid, {"n": 0, "weight": None, "symbols": set()})
            g["n"] += 1
            # weight 取末次覆盖：单实验内 weight 恒定，跨 plan 会随 promote/set-weight 更新
            g["weight"] = o.get("experiment_weight")
            g["symbols"].add(o["order"]["symbol"])

    # 表头：定宽对齐方便终端肉眼扫读
    print(f"{'experiment_id':30}{'订单数':>8}{'权重':>8}{'标的数':>8}")
    for eid, g in sorted(groups.items()):
        w = f"{g['weight']:.2f}" if g["weight"] is not None else "-"
        print(f"{eid:30}{g['n']:>8}{w:>8}{len(g['symbols']):>8}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="experiment", description="实验系统配置中心")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create", help="创建 DRAFT 版本")
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--params", required=True, help="JSON 参数串")
    sp.add_argument("--experiment-id", required=True)
    sp.add_argument("--version", type=int, default=1)
    sp.add_argument("--source", default="manual")
    sp.add_argument("--note", default="")
    sp.add_argument("--created-at", default=None)

    sp = sub.add_parser("promote", help="DRAFT→ACTIVE + 设权重")
    sp.add_argument("experiment_id")
    sp.add_argument("--weight", type=float, required=True)

    sp = sub.add_parser("set-weight", help="调整 ACTIVE 权重")
    sp.add_argument("experiment_id")
    sp.add_argument("--weight", type=float, required=True)

    sp = sub.add_parser("archive", help="ACTIVE→ARCHIVED")
    sp.add_argument("experiment_id")

    sp = sub.add_parser("rollback", help="ARCHIVED→ACTIVE")
    sp.add_argument("experiment_id")

    sub.add_parser("list", help="列所有版本")

    # report：读 DB SIGNAL.meta 按 experiment_id 聚合归因（事后审计 prod vs candidate）
    sp = sub.add_parser("report", help="读 DB 计划按 experiment_id 聚合归因")
    sp.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD（含），留空全量")
    return p


def main(argv: list = None) -> int:
    """CLI 入口（返回退出码）。db 路径由模块级 _DEFAULT_DB 决定（测试 monkeypatch）。"""
    args = _build_parser().parse_args(argv)
    db = _DEFAULT_DB
    try:
        if args.cmd == "create":
            v = ExperimentVersion(
                experiment_id=args.experiment_id, strategy_name=args.strategy,
                params=json.loads(args.params), weight=0.0, status=ExperimentStatus.DRAFT,
                version=args.version, source=args.source, note=args.note,
                created_at=args.created_at or _now())
            create_version(db, v, operator=_OPERATOR)
            print(f"created {args.experiment_id} (DRAFT)")
        elif args.cmd == "promote":
            _promote(db, args.experiment_id, weight=args.weight, operator=_OPERATOR, now=_now())
            print(f"promoted {args.experiment_id} weight={args.weight}")
        elif args.cmd == "set-weight":
            set_weight(db, args.experiment_id, new_weight=args.weight, operator=_OPERATOR, now=_now())
            print(f"set-weight {args.experiment_id} → {args.weight}")
        elif args.cmd == "archive":
            _archive(db, args.experiment_id, operator=_OPERATOR, now=_now())
            print(f"archived {args.experiment_id}")
        elif args.cmd == "rollback":
            _rollback(db, args.experiment_id, operator=_OPERATOR, now=_now())
            print(f"rollback {args.experiment_id} → ACTIVE")
        elif args.cmd == "list":
            for v in list_versions(db):
                print(f"{v.experiment_id:30} {v.strategy_name:10} {v.status.value:9}"
                      f" w={v.weight:.2f} v={v.version} src={v.source}")
        elif args.cmd == "report":
            # report 不读 db，纯扫 plan 文件聚合
            return _report(args)
        return 0
    except ValueError as e:
        # 状态机/权重校验失败：stderr + 非零退出（绝不静默改一半）
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
