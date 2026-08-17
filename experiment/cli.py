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
from experiment.store import _DEFAULT_DB, archive as _archive, create_version, discard as _discard, list_versions, promote as _promote, rollback as _rollback, set_weight

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

    sp = sub.add_parser("discard", help="DRAFT→ARCHIVED（DRAFT 池处置出口，仅 DRAFT）")
    sp.add_argument("experiment_id")
    sp.add_argument("--note", default="", help="处置原因（写审计，如「autopromote G2 未过」）")

    # autopromote（T2.3 · ADR-15）：七门量化门槛自动 promote。默认 dry-run（只评估+播报，
    # 零写库）；--exec 才写库且受 env AUTO_PROMOTE_ENABLED 总开关管辖（fail-closed）。
    # experiment_id 可选（review 修复：T3.4 日报 cron 只传 --latest 不带 id，原必填位置
    # 参数会让 argparse exit 2、--latest 分支不可达——cron 从未跑通过）。
    sp = sub.add_parser("autopromote", help="七门量化门槛自动 promote（默认 dry-run）")
    sp.add_argument("experiment_id", nargs="?", default=None,
                    help="候选 experiment_id（--latest 时可省略）")
    sp.add_argument("--confirm", action="store_true",
                    help="灰度第二步：候选 weight→1.0 + 基线 archive（跳过评估）")
    sp.add_argument("--weight", type=float, default=None, help="灰度第一步新权重（默认 0.3）")
    sp.add_argument("--exec", dest="execute", action="store_true",
                    help="实际写库（默认 dry-run 只评估播报）")
    sp.add_argument("--baseline", default=None, help="基线 experiment_id（默认当前 ACTIVE 冠军）")
    sp.add_argument("--latest", action="store_true",
                    help="自动选取最新 DRAFT（T3.4 日报 cron 用；无 DRAFT 时静默退出 0）")

    sp = sub.add_parser("rollback", help="ARCHIVED→ACTIVE")
    sp.add_argument("experiment_id")

    sub.add_parser("list", help="列所有版本")

    # report：plan 归因已下沉 trading 层（C2d · 2026-08-12）。本子命令保留维持 argparse
    # 兼容，main 分支仅打 deprecation 提示（experiment 是叶子层，禁 trading import）。
    sp = sub.add_parser("report", help="[已废弃] plan 归因迁移至 trading 层")
    sp.add_argument("--since", default=None, help="[已废弃] 起始日期 YYYY-MM-DD（含）")
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
            # ADR-16 修订（2026-08-17）：影子期闸移除后新实验上线只播报不拦截。
            # Review 修复：必须先 build_default_manager() 装配——CLI 独立进程的
            # get_default() 是裸单例（零通道），不装配时 notify 静默空转且 except 不
            # 触发（表面完全正常）。同步 asyncio.run（CLI 短命进程，fire_and_forget
            # 的 daemon 线程来不及发）；通道非空断言 + 播报失败均不阻断 promote 返回码。
            try:
                import asyncio as _aio
                from infra.notifier import NotificationManager, build_default_manager
                build_default_manager()   # 幂等装配（读 .env 钉钉配置建通道）
                _mgr = NotificationManager.get_default()
                if not _mgr._channels:
                    print("（⚠ 上线播报未发：钉钉通道未装配（查 .env DINGTALK_* 配置）——promote 已成功）")
                else:
                    _aio.run(_mgr.notify_risk_event(
                        f"实验上线：{args.experiment_id} → ACTIVE（weight={args.weight}）——"
                        f"影子期闸已移除（ADR-16 修订），新参数即刻生效；"
                        f"如需缓冲请 risk_ctrl block on", "WARN"))
            except Exception:
                print("（上线播报发送失败，promote 本身已成功）")
        elif args.cmd == "set-weight":
            set_weight(db, args.experiment_id, new_weight=args.weight, operator=_OPERATOR, now=_now())
            print(f"set-weight {args.experiment_id} → {args.weight}")
        elif args.cmd == "archive":
            _archive(db, args.experiment_id, operator=_OPERATOR, now=_now())
            print(f"archived {args.experiment_id}")
        elif args.cmd == "discard":
            _discard(db, args.experiment_id, operator=_OPERATOR, now=_now(),
                     note=args.note)   # review 修复：--note 落审计（原死参数）
            print(f"discarded {args.experiment_id}")
        elif args.cmd == "autopromote":
            # lazy import（仿 discovery/cli.py _auto_publish 范式）：experiment 是叶子层，
            # 顶层 import research.autopromote 会成环（research→experiment）。
            from research.autopromote import run as _autopromote_run
            target = args.experiment_id
            if args.latest:
                drafts = [v for v in list_versions(db)
                          if v.status == ExperimentStatus.DRAFT]
                if not drafts:
                    print("无 DRAFT——日报跳过")
                    return 0
                target = drafts[-1].experiment_id   # created_at 序的最末=最新
            elif target is None:
                # review 修复：既无 id 又无 --latest → 显式报错（argparse 层已不拦，
                # 此处 fail-fast 给人看清楚用法，而非 KeyError 埋雷）
                print("错误: 需要 experiment_id 或 --latest", file=sys.stderr)
                return 1
            res = _autopromote_run(target,
                                   phase="confirm" if args.confirm else "initial",
                                   dry_run=not args.execute,
                                   weight=args.weight, baseline_id=args.baseline)
            print(json.dumps(res, ensure_ascii=False, default=str, indent=1))
        elif args.cmd == "rollback":
            _rollback(db, args.experiment_id, operator=_OPERATOR, now=_now())
            print(f"rollback {args.experiment_id} → ACTIVE")
        elif args.cmd == "list":
            for v in list_versions(db):
                print(f"{v.experiment_id:30} {v.strategy_name:10} {v.status.value:9}"
                      f" w={v.weight:.2f} v={v.version} src={v.source}")
        elif args.cmd == "report":
            # C2d（2026-08-12）：plan 归因已下沉 trading 层（plan 真相源=DB trade_event，
            # experiment 是叶子层禁 trading import）。本子命令保留以维持 argparse 兼容，
            # 仅打印迁移提示，不聚合。
            print("plan 归因已迁移至 trading 层，请改用：python -m trading.plan_report --since YYYY-MM-DD")
            return 0
        return 0
    except ValueError as e:
        # 状态机/权重校验失败：stderr + 非零退出（绝不静默改一半）
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
