# -*- coding: utf-8 -*-
"""命令行：python -m discovery {oos,verify}。

oos：对当前 param_iter 冠军（logs/param_iter_state.json 的 best）跑 2025/2026 holdout
嵌套评估，固化其 2026 去偏水平（L1 验收锚），落 SQLite。解决探查实证的快照漂移。

物理意图：探查脚本 probe_champion_oos.py 因 data_lake 增量 + 流动性边界票浮动，两次跑
universe 漂 6%——"连复现自己都做不到"。本 cli 把"冠军 2026 去偏水平"固化成一个带快照
指纹 + 引擎指纹的 trial 记录，后续内核/数据变了老 trial 自动标 stale（engine_hash/
snapshot_hash 双指纹），给 L1 验收一个不漂移的锚。
"""
import argparse
import hashlib
import json
import os

from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate
from discovery.store import (init_db, connect, write_snapshot, write_trial,
                             trial_id_of, DEFAULT_DB_PATH)
from discovery.judging import feasibility_gate

STATE_FILE = "logs/param_iter_state.json"


def _db_path():
    """环境变量 DISCOVERY_DB 覆盖 DEFAULT_DB_PATH（测试隔离用，避免污染 logs 库）。"""
    return os.environ.get("DISCOVERY_DB", DEFAULT_DB_PATH)


def _engine_hash():
    """回测内核代码 hash（backtest.py+method_v0.py 内容 sha256[:12]）。

    物理意图：内核改了老 trial 的指标就 stale 了——engine_hash 作内核指纹，与
    snapshot_hash 双指纹共同标识"可复现试验"。内核（scan_symbol/risk_metrics）一动，
    engine_hash 变，老 trial 自然与新跑不可比（spec §3.2 engine_hash）。
    """
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def cmd_oos(args):
    """当前冠军 2026 去偏评估 + 落库。

    串起 freeze→holdout_split→evaluate→judging→store 全链。冠军从 param_iter_state.json
    的 best 读（与探查脚本同源），跑一次全历史 scan，分 inner(2025)/outer(2026) 两段报指标。
    outer 不反馈任何选择（冠军已由 param_iter 用 2025+2026 全段 score 选出——诚实标注见下）。
    """
    universe, meta = freeze()
    split = holdout_split(args.embargo)
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    params = state["best"]
    print(f"=== discovery oos：当前冠军 2026 去偏（snapshot={meta.snapshot_hash}）===")
    print(f"snapshot: {meta.universe_count} 只 | {meta.date_range} | hash {meta.snapshot_hash}")
    res = evaluate(params, universe, split)
    _print_segment("inner 2025", res["inner"])
    _print_segment("★outer 2026", res["outer"])
    print(f"L0 可行域闸(inner): {'通过' if feasibility_gate(res['inner']) else '不通过'} "
          f"(熊市 ann≥0 在 Plan 1 N/A——2025-2026 无熊市数据)")
    print(f"诚实标注: 2026 非纯 OOS（冠军用 2025+2026 全段 score 选出，2026 参与了选择）；"
          f"夏普/ann 是 risk_metrics 复利放大产物，绝对值非实盘预期")
    db = _db_path()
    init_db(db)
    eng = _engine_hash()
    tid = trial_id_of(params, meta.snapshot_hash)
    with connect(db) as conn:
        write_snapshot(conn, meta)
        write_trial(conn, tid, params, meta.snapshot_hash, eng, "holdout_2025_2026",
                    res["inner"], res["outer"], "manual_champion")
    print(f"落库: db={db} trial_id={tid} engine_hash={eng}")


def _print_segment(name, m):
    """打印一段指标（ann/calmar/夏普/回撤/笔数对齐，供人眼快速判读）。"""
    print(f"{name:>12}: ann {m['ann']*100:>6.1f}%  calmar {m['calmar']:>5.2f}  "
          f"夏普{m['sharpe']:>5.2f}  回撤{m['max_dd']*100:>5.1f}%  {m['n']:>5}笔")


def main(argv=None):
    """cli 入口：子命令派发。argv=None 走 sys.argv（python -m discovery oos）。"""
    ap = argparse.ArgumentParser(prog="discovery")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_oos = sub.add_parser("oos", help="当前冠军 2026 去偏评估（L1 验收锚）")
    ap_oos.add_argument("--embargo", type=int, default=5, help="inner→outer embargo 天数")
    ap_oos.set_defaults(func=cmd_oos)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
