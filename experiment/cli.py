# -*- coding: utf-8 -*-
"""实验系统 CLI：python -m experiment create|promote|set-weight|archive|rollback|list|report

每个命令操作 experiment/experiments.db，变更写审计。退出码：0 成功 / 非 0 失败。
now 时间戳由调用方传或取当前；测试传固定值保证可复现。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from experiment.models import ExperimentVersion, ExperimentStatus
from experiment.store import _DEFAULT_DB, archive as _archive, create_version, list_versions, promote as _promote, rollback as _rollback, set_weight

_OPERATOR = "cli"


def _now() -> str:
    """当前 ISO 时间戳（CLI 实跑用；测试走 store 层固定 now）。"""
    return datetime.now().isoformat(timespec="seconds")


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
        return 0
    except ValueError as e:
        # 状态机/权重校验失败：stderr + 非零退出（绝不静默改一半）
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
