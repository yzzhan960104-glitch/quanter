# -*- coding: utf-8 -*-
"""A3 实证：成交真实性/滑点敏感性——冠军与最强 DRAFT 在摩擦成本下的衰减曲线。

物理意图（audit spec A 波 A3，roadmap 最高优先空洞）：
    颈线法回测的乐观偏差来源之一是成交假设——止损按目标价"完美成交"，跳空/跌停
    封死未建模。PositionModel.slippage_bps（双边，逐笔从收益扣 s×2/1e4）是现有
    唯一的摩擦成本口径：本脚本以滑点为代理变量扫 {0,5,10,20,50}bps，量化
    「策略真实边际 vs 摩擦成本」——outer 段 ann 衰减到 0 的位置=盈亏平衡滑点。

判据（写进报告 docs/research/2026-08-16-a3-fill-realism.md）：
    outer ann@10bps ≥ ann@0bps×50% 且 ann@20bps > 0 → 成本敏感可存活；
    否则"薄边缘"——触发 autopromote G7 常量收紧与搜索默认滑点上调决策。

口径对齐：
    · 参数集=experiments.db 直读（ACTIVE 25c602 + DRAFT 47d350，同源单一真相）；
    · lake_start=2021-01-01（保证 2025 信号 window/ATR 预热完整，与 daemon 扩展口径同源）；
    · 切分=holdout_split（inner 2025 / outer 2026，embargo=5）；
    · 引擎=evaluate_replay（backtest.replay 主回测同源，position_model 白名单注入滑点）。
    · 滑点只作用 equity 口径（ann/max_dd 衰减），win_rate/avg_rr 为信号 rr 口径不变——
      表格两口径并列正是"识别质量 vs 落地成本"的分界观察面。

用法（后台跑，预计 1-1.5h）：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/a3_fill_realism_probe.py
"""
import sys, os, time, json, sqlite3, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate_replay

# ── 参数集：默认 ACTIVE；R1 起 --experiments 指定候选（逗号分隔 experiment_id）──
# R1-3（2026-08-16）：G7 翻绿的验体从「当任 ACTIVE」改为「当前候选」，支持
# autopromote 冠军候选的滑点存活复验。
_ap = argparse.ArgumentParser()
_ap.add_argument("--experiments", default="",
                 help="逗号分隔 experiment_id 列表（默认仅 ACTIVE 基线）")
_ARGS, _ = _ap.parse_known_args()

conn = sqlite3.connect("experiment/experiments.db")
conn.row_factory = sqlite3.Row
PARAM_SETS = {}
row = conn.execute(
    "SELECT experiment_id, params FROM experiment_version WHERE status='ACTIVE' "
    "ORDER BY weight DESC LIMIT 1").fetchone()
PARAM_SETS["ACTIVE_" + row["experiment_id"].split("_")[-1]] = json.loads(row["params"])
for eid in filter(None, _ARGS.experiments.split(",")):
    r2 = conn.execute(
        "SELECT experiment_id, params FROM experiment_version WHERE experiment_id=?",
        (eid.strip(),)).fetchone()
    if r2 is not None:
        PARAM_SETS[eid.split("_")[-1]] = json.loads(r2["params"])

SLIPPAGES = [0.0, 5.0, 10.0, 20.0, 50.0]   # bps（双边口径：每边扣 s，逐笔共 s×2）

# ── 快照冻结（一次加载，全部 run 复用；meta 指纹随报告落盘保可复现）──
t0 = time.time()
universe, meta = freeze("2021-01-01")
split = holdout_split()
print(f"[freeze] universe={meta.universe_count} hash={meta.snapshot_hash} "
      f"用 {time.time()-t0:.0f}s\n", flush=True)

results = {}   # {(label, s): {"inner": {...}, "outer": {...}}}
for label, params in PARAM_SETS.items():
    for s in SLIPPAGES:
        t1 = time.time()
        res = evaluate_replay(params, universe, split,
                              position_model={"slippage_bps": s})
        results[(label, s)] = res
        i, o = res["inner"], res["outer"]
        print(f"[{label} | slip={s:.0f}bps] "
              f"inner: n={i['n_hits']} ann={i['annualized_return']:+.1%} "
              f"dd={i['max_drawdown']:.1%} wr={i['win_rate']:.1%} | "
              f"outer: n={o['n_hits']} ann={o['annualized_return']:+.1%} "
              f"dd={o['max_drawdown']:.1%} wr={o['win_rate']:.1%} "
              f"| 用 {time.time()-t1:.0f}s", flush=True)

# ── 元数据 run：止损跳空/同日双向等执行假设计数量化（A3 的另一半证据）──
print("\n[exec-stats] ACTIVE 参数 outer 2026 段 strategy_stats（止损跳空/同日双向计数）",
      flush=True)
try:
    from backtest.replay import replay
    from backtest.models import PositionModel
    from strategies.neckline.strategy import NecklineMethodStrategy
    active_params = list(PARAM_SETS.values())[0]
    rep = replay(universe, NecklineMethodStrategy(cfg_override=active_params),
                 str(split.outer.start), str(split.outer.end),
                 position_model=PositionModel())
    stats_json = json.dumps(rep.metadata.get("strategy_stats", {}),
                            ensure_ascii=False, default=str)
    print(f"  strategy_stats={stats_json}", flush=True)
    print(f"  n_exceptions={rep.metadata.get('n_exceptions')} "
          f"degraded={rep.metadata.get('degraded')}", flush=True)
except Exception:
    import traceback
    traceback.print_exc()

# ── 判定：盈亏平衡滑点（ann 线性过零插值）+ 10bps 存活率 ──
print("\n=== A3 判定 ===", flush=True)
try:
    from discovery.fingerprint import engine_hash
    print(f"engine_hash={engine_hash()}", flush=True)
except Exception:
    pass

def _breakeven(anns):
    """相邻档 ann 符号翻转处线性插值 → 盈亏平衡滑点（bps）。全正返 >50，全负返 0。"""
    for (s0, a0), (s1, a1) in zip(anns, anns[1:]):
        if a0 > 0 and a1 <= 0:
            return s0 + (s1 - s0) * a0 / (a0 - a1)
    return 51.0 if anns[0][1] > 0 else 0.0

for label in PARAM_SETS:
    outer_anns = [(s, results[(label, s)]["outer"]["annualized_return"])
                  for s in SLIPPAGES]
    inner_anns = [(s, results[(label, s)]["inner"]["annualized_return"])
                  for s in SLIPPAGES]
    ann0 = results[(label, 0.0)]["outer"]["annualized_return"]
    ann10 = results[(label, 10.0)]["outer"]["annualized_return"]
    ann20 = results[(label, 20.0)]["outer"]["annualized_return"]
    survive = (ann0 > 0 and ann10 >= ann0 * 0.5 and ann20 > 0)
    print(f"[{label}] outer 盈亏平衡滑点≈{_breakeven(outer_anns):.1f}bps "
          f"(inner≈{_breakeven(inner_anns):.1f}bps) | "
          f"outer ann 0→10bps: {ann0:+.1%}→{ann10:+.1%} "
          f"(存活率 {ann10/ann0 if ann0 > 0 else float('nan'):.0%}) | "
          f"判定={'可存活' if survive else '薄边缘'}", flush=True)

# 原始数字落盘（报告素材，防终端截断丢精度）
out_path = os.path.join("logs", "a3_fill_realism_results.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({f"{l}|{s}": v for (l, s), v in results.items()}, f,
              ensure_ascii=False, indent=1, default=str)
print(f"\n[done] 原始数字 → {out_path}", flush=True)
