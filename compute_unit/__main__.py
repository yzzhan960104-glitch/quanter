# -*- coding: utf-8 -*-
"""compute_unit CLI:verify / run / summary 三子命令。

物理定位:Mac 端唯一入口。verify=只校验不跑;run=校验+跑批+写 result.json;
summary=读 result.json 生成摘要文本(spec §6 数据流出站前最后一步,人 AirDrop 前生成)。

退出码:0=成功;3=环境漂移(EnvDriftError);1=其他错误。
"""
from __future__ import annotations

import argparse
import sys

# 通过模块属性访问(而非 `from X import Y` 直接绑定名字),让 monkeypatch 改
# compute_unit.env_check.verify / compute_unit.runner.run 在测试里即时生效。
# 这是 Python 测试惯例:被 mock 的对象须按"模块属性"动态查找。
from compute_unit import env_check, protocol, runner, summary
from compute_unit.env_check import EnvDriftError   # 异常类是常量,直接导入无碍


def main(argv: list[str] | None = None) -> int:
    """CLI 总入口。返 0=成功 / 3=环境漂移 / 1=其他错误。"""
    # stdout UTF-8 治理:防 GBK 管道崩 emoji(详见 infra/pyio.py)
    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    p = argparse.ArgumentParser(prog="python -m compute_unit", description="Mac 远程计算单元(回测)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("verify", help="校验 task.json 环境(三件哈希+snapshot,不跑批)")
    pv.add_argument("task_json", help="task.json 路径")

    pr = sub.add_parser("run", help="校验 + 跑批 → 写 result.json")
    pr.add_argument("task_json", help="task.json 路径")
    pr.add_argument("-o", "--out", default="result.json", help="result.json 输出路径(默认 ./result.json)")
    pr.add_argument("--n-proc", type=int, default=None, help="并发进程数(默认核数-2)")

    ps = sub.add_parser("summary", help="读 result.json → top-N 中文摘要")
    ps.add_argument("result_json", help="result.json 路径")
    ps.add_argument("--top", type=int, default=3, help="展示 top-N(默认 3)")

    args = p.parse_args(argv)

    if args.cmd == "verify":
        task = protocol.Task.from_json(args.task_json)
        try:
            env_check.verify(task)
        except EnvDriftError as e:
            print(f"❌ 环境漂移:{e}", file=sys.stderr)
            return 3
        print(f"✅ 环境一致,可跑批(task={task.task_id})")
        return 0

    if args.cmd == "run":
        task = protocol.Task.from_json(args.task_json)
        try:
            result = runner.run(task, n_proc=args.n_proc)
        except EnvDriftError as e:
            print(f"❌ 环境漂移:{e}", file=sys.stderr)
            return 3
        result.to_json(args.out)
        print(f"✅ 跑批完成 task={result.task_id} → {args.out}({len(result.results)}组)")
        return 0

    if args.cmd == "summary":
        result = protocol.Result.from_json(args.result_json)
        print(summary.summarize(result, top_n=args.top))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
