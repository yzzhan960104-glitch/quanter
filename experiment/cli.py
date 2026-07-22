# -*- coding: utf-8 -*-
"""实验系统 CLI：python -m experiment create|promote|set-weight|archive|rollback|list|report

每个命令操作 experiment/experiments.db，变更写审计。退出码：0 成功 / 非 0 失败。
now 时间戳由调用方传或取当前；测试传固定值保证可复现。
"""
from __future__ import annotations

import argparse
import glob
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
    """扫描交易计划目录下所有 plan_*.json，返回 plan dict 列表。

    Why：trading_plan 每日落一个 JSON，内部 orders[] 每单带 experiment_id/experiment_weight；
    report 子命令要按实验维度聚合归因，必须先把全量 plan 读出来供下游分组。
    设计要点：
    - 目录默认 logs/trading_plans，可由环境变量 TRADE_PLAN_DIR 覆盖（本地/CI 切换）。
    - 文件名按 plan_<date>.json 命名，glob 出来再 sorted 保证可复现。
    - since 走日期字符串字典序比较：因 date 是 ISO 格式（YYYY-MM-DD），字典序==时间序，
      简单且零依赖，避免引入 dateutil 这类黑盒。
    - 单文件解析失败静默跳过：脏 plan 不能让整条归因链路炸掉（防御性，老 plan 漂移常见）。
    """
    plan_dir = os.getenv("TRADE_PLAN_DIR", "logs/trading_plans")
    plans = []
    for path in sorted(glob.glob(os.path.join(plan_dir, "plan_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                p = json.load(f)
            # 字典序比较 ISO 日期：since 含日以上的精度都能正确裁剪
            if since and p.get("date", "") < since:
                continue
            plans.append(p)
        except Exception:
            # 脏 plan（非合法 JSON / 缺字段 / 编码异常）跳过，不让整盘归因失败
            continue
    return plans


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

    # report：扫 logs/trading_plans 按 experiment_id 聚合归因（事后审计 prod vs candidate）
    sp = sub.add_parser("report", help="扫 trading_plans 按 experiment_id 聚合归因")
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
