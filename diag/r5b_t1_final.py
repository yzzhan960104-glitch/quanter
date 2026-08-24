# -*- coding: utf-8 -*-
"""R5b 终验：tp_h=0.8（T+1 修复后 +392.6%）的 0.7 补档 + wf 四折 + 滑点敏感性。"""
import json
import os
import sys
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def main():
    from discovery.snapshot import freeze
    from discovery.split import walk_forward_split, holdout_split
    from discovery.objective import evaluate_portfolio
    from discovery.manual_risk_sim import build_block_calendar
    from backtest.replay import replay
    from strategies.neckline.strategy import NecklineMethodStrategy

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    universe, meta = freeze("2021-01-01")
    split = holdout_split()
    cal = build_block_calendar(universe)

    # ① 0.7 补档（组合口径）
    for tph in (0.7, 0.65):
        p = {**deepcopy(base), "tp_h_mult": tph}
        r = evaluate_portfolio(p, universe, split, block_dates=cal)
        print(f"tp_h={tph}: raw {r['outer_raw']['ann']:+.1%} | inner n={r['inner']['n']}", flush=True)

    # ② 修复后 wf 四折（tp_h=0.8）
    p8 = {**deepcopy(base), "tp_h_mult": 0.8}
    wf = walk_forward_split()
    print("=== tp_h=0.8（T+1 修复后）wf 四折 ===", flush=True)
    for name, train, oos in wf.folds:
        strat = NecklineMethodStrategy(cfg_override=p8)
        rep = replay(universe, strat, str(oos.start), str(oos.end))
        ann, dd = rep.annualized_return, rep.max_drawdown
        print(f"  {name} (oos {oos.start}): raw ann {ann:+6.1%} dd {dd:5.1%} n={rep.n_hits}", flush=True)

    # ③ 滑点敏感性：10/20bps 下的 outer（PositionModel 实例直传——dict 会炸
    # build_equity_curve 的 model.risk_frac 属性访问）
    from backtest.models import PositionModel
    print("=== tp_h=0.8 滑点敏感性（raw outer）===", flush=True)
    for bps in (5, 10, 20, 30):
        r = evaluate_portfolio(p8, universe, split,
                               position_model=PositionModel(slippage_bps=bps), block_dates=None)
        print(f"  slip {bps}bps: raw outer {r['outer_raw']['ann']:+.1%}", flush=True)
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
