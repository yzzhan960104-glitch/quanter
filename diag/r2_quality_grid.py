# -*- coding: utf-8 -*-
"""R2-A 实证：质量邻域 168 格系统化网格——G1 矛盾的正面裁决数据（2026-08-22）。

物理意图（R1 结论驱动，ROUND_LOG R2 计划第 1 条）：
    R1 证明组合口径下正边缘方向是「质量优先」（3e383d/touch3 均为收紧信号闸的
    产物），但双双卡在 G1 inner n≥100（73/85）——「质量收紧天然减样本」与门槛的
    结构性矛盾没有数据就无法裁决。本网格把 R1 的单维探测扩展为甜点区系统化：
        - max_h_atr 7 档细分（2.3-3.0，R1 实证 2.0 全灭/2.5 精选/3.0 泛滥的甜点
          区内部加密，G1 矛盾的答案大概率藏在这根轴上：质量闸每松一档 n 涨多少、
          ann 塌多少）；
        - min_touches×window×min_suppression×breakout_vol_mult 的 24 种组合
          （touch3×win80 是 R2 计划点名格；vol2.0/supp0.7 R1 实证反噬不进）。
    168 格 = 7×2×3×2×2，其余维度全部钉 3e383d 基线（执行层钉实弹退化态——
    问题在信号质量不在出场，R0 已证）。

信息隔离（协议 §二军规）：选择只看 inner（2025），outer（2026）只进报告不反馈
选择。口径：evaluate_replay（replay 引擎=实盘同源组合口径），滑点 5bps 默认。

并行形态（Why 每 worker 自 freeze 而非主进程 pickle 大 DataFrame）：Windows
spawn 模式下跨进程传 463MB 湖对象每 worker 一次序列化，不如让各 worker 读
parquet（OS page cache 二次命中后 ~秒级），内存驻留 6×~2GB 在 32G 机器安全。

用法（后台跑；smoke 模式 3 格串行供单格成本标定）：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r2_quality_grid.py smoke
    PYTHONIOENCODING=utf-8 nohup .venv310/Scripts/python.exe -u diag/r2_quality_grid.py 6 \
        > logs/r2_quality_grid.log 2>&1 &
"""
import itertools
import json
import os
import sqlite3
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate_replay

# ── 基线锚：3e383d（R0/R1 唯一 replay 正边缘链，r1-repair 已物化 21 键全参）──
conn = sqlite3.connect("experiment/experiments.db")
conn.row_factory = sqlite3.Row
BASE = json.loads(conn.execute(
    "SELECT params FROM experiment_version WHERE experiment_id='neckline_prop_20260816_3e383d'"
).fetchone()["params"])
ACTIVE = json.loads(conn.execute(
    "SELECT params FROM experiment_version WHERE status='ACTIVE' ORDER BY weight DESC LIMIT 1"
).fetchone()["params"])
conn.close()

# 网格（R2 甜点区）：核心 5 维，其余维钉 BASE。168 格。
_MH = (2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.0)      # max_h_atr：R1 甜点区 7 档细分
_MT = (2, 3)                                    # min_touches：touch3 方向 × base
_W = (40, 60, 80)                               # window：R2 点名 win80 + 短沿 40
_MS = (0.5, 0.6)                                # min_suppression（0.7 反噬不进）
_BV = (1.0, 1.5)                                # breakout_vol_mult（2.0 反噬不进）
GRID = [
    (f"mh{mh}/t{mt}/w{w}/s{ms}/v{bv}",
     {"max_h_atr": mh, "min_touches": mt, "window": w,
      "min_suppression": ms, "breakout_vol_mult": bv})
    for mh, mt, w, ms, bv in itertools.product(_MH, _MT, _W, _MS, _BV)
]

# 参照锚（与网格同口径同批次重跑，防跨批次漂移）：3e383d / touch3 / ACTIVE
ANCHORS = [
    ("*base_3e383d", {}),
    ("*touch3", {"min_touches": 3}),
    ("*active_25c602", ACTIVE),
]


def _init_worker():
    """worker 初始化：各自 freeze（page cache 复用）——全局变量供 task 函数消费。"""
    global _universe, _split, _meta
    t0 = time.time()
    _universe, _meta = freeze("2021-01-01")
    _split = holdout_split()
    print(f"  [worker {os.getpid()}] freeze 就绪 universe={_meta.universe_count} "
          f"hash={_meta.snapshot_hash} 用 {time.time()-t0:.0f}s", flush=True)


def _eval_one(item):
    """单格评估（worker 进程内）：override 合成全参 → evaluate_replay 组合口径。"""
    label, override = item
    params = {**BASE, **override}
    t1 = time.time()
    res = evaluate_replay(params, _universe, _split)
    i, o = res["inner"], res["outer"]
    print(f"[{label:>22}] inner: n={i['n_hits']:>3} ann={i['annualized_return']:+7.1%} "
          f"dd={i['max_drawdown']:6.1%} wr={i['win_rate']:5.1%} | "
          f"outer: n={o['n_hits']:>3} ann={o['annualized_return']:+7.1%} "
          f"dd={o['max_drawdown']:6.1%} | 用 {time.time()-t1:.0f}s", flush=True)
    return label, {"override": override, "inner": i, "outer": o}


def main() -> int:
    smoke = "smoke" in sys.argv
    n_proc = 1 if smoke else int(next(
        (a for a in sys.argv[1:] if a.isdigit()), 6))
    tasks = ([("*smoke_anchor", {})] + GRID[:2]) if smoke else ANCHORS + GRID
    print(f"[r2_quality_grid] 格数={len(tasks)} 并行={n_proc} "
          f"（smoke={smoke}）", flush=True)

    t0 = time.time()
    results = {}
    if n_proc <= 1:
        _init_worker()                            # 冒烟：主进程串行（成本标定口径）
        for item in tasks:
            label, r = _eval_one(item)
            results[label] = r
    else:
        with Pool(processes=n_proc, initializer=_init_worker) as pool:
            for label, r in pool.imap_unordered(_eval_one, tasks):
                results[label] = r
    print(f"\n[r2_quality_grid] 全部完成 用 {(time.time()-t0)/60:.0f}min", flush=True)

    # ── 判定（信息隔离：inner 排序选参；outer 只报告）──
    print("\n=== R2-A 判定（inner ann 排序；✓=n≥100 过 G1 门槛）===", flush=True)
    try:
        from discovery.fingerprint import engine_hash
        print(f"engine_hash={engine_hash()}", flush=True)
    except Exception:
        pass
    ranked = sorted(
        ((k, v) for k, v in results.items() if not k.startswith("*")),
        key=lambda kv: kv[1]["inner"]["annualized_return"], reverse=True)
    for label, r in ranked[:25]:
        i, o = r["inner"], r["outer"]
        n_ok = "✓" if i["n_hits"] >= 100 else ("✗n<30" if i["n_hits"] < 30 else "△n<100")
        print(f"  {label:>22}: inner ann {i['annualized_return']:+7.1%} n={i['n_hits']:>3} {n_ok}"
              f" | outer ann {o['annualized_return']:+7.1%} dd {o['max_drawdown']:6.1%}", flush=True)
    # G1 矛盾裁决视图：n≥100 的格里 inner ann 最高者（存在性证明/证伪的直接证据）
    n100 = [(k, v) for k, v in ranked if v["inner"]["n_hits"] >= 100]
    print(f"\n[G1 裁决] n≥100 的格共 {len(n100)} 个；"
          + (f"最高 inner ann={n100[0][1]['inner']['annualized_return']:+.1%}"
             f"（{n100[0][0]}）" if n100 else "不存在——G1=100 与质量方向矛盾实锤，需裁决"), flush=True)

    out_path = os.path.join("logs", "r2_quality_grid_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n[done] 原始数字 → {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
