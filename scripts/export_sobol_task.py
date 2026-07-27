# -*- coding: utf-8 -*-
"""Win 端 Phase 2 工具:discovery Sobol+随机采样 → 导出 task.json(Mac 拉取跑批)。

封装 sample_search → compute_unit.task_export。Sobol 在 21 维离散空间映射重复率高
(实测 3000 Sobol → 531 唯一),故循环采样(Sobol+随机混合,seed 递增)直到凑够 N 唯一组。

用法:
  python scripts/export_sobol_task.py --n 3000
  python scripts/export_sobol_task.py --n 5000 --seed 123 --out tasks/big.json

算力估算(memory:单组 evaluate ~720s,Mac M1Max 默认 8 进程并发):
  N 组 ≈ N × 720s / 8 进程
  3000 组 ≈ 3 天 | 5000 组 ≈ 5 天(Mac 若比 Win 快则更长)
"""
import argparse
import json
import sys
from pathlib import Path

# Windows GBK console 兜底:强制 stdout UTF-8(否则 print emoji/中文报 UnicodeEncodeError)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 锁项目根(compute_unit/discovery 的 import 基准)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compute_unit.task_export import export_task
from discovery.sampler import sample_search


def collect_params(target_n, seed, sobol_per_round, random_per_round, max_rounds=10):
    """循环 sample_search 直到去重后凑够 target_n 唯一组。

    Sobol 离散映射重复高(准随机聚集),单轮不够;每轮 seed 递增(scramble 不同)+
    Sobol+随机混合(随机部分几乎全唯一,补 Sobol 的聚集)。每轮去重累积。
    """
    seen, all_params, rounds = set(), [], 0
    while len(all_params) < target_n and rounds < max_rounds:
        rounds += 1
        # seed 每轮 +7919(质数偏移,让 Sobol scramble 与随机起点都不同)
        batch = sample_search(n_sobol=sobol_per_round, n_random=random_per_round,
                              seed=seed + rounds * 7919)
        for p in batch:
            k = json.dumps(p, sort_keys=True, ensure_ascii=False, default=str)
            if k not in seen:
                seen.add(k)
                all_params.append(p)
        print(f"  轮 {rounds}: +{len(batch)} 采样 → 累计唯一 {len(all_params)} / {target_n}")
    if len(all_params) < target_n:
        print(f"  ⚠ {max_rounds} 轮仅得 {len(all_params)} 唯一(离散空间或采样不足),用 {len(all_params)} 组")
    return all_params[:target_n]


def main():
    ap = argparse.ArgumentParser(description="Sobol+随机采样 → 导出 task.json(Mac 跑批用)")
    ap.add_argument("--n", type=int, default=3000, help="目标唯一组数(默认 3000≈Mac 3 天)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sobol-per-round", type=int, default=500, help="每轮 Sobol 采样数(默认 500)")
    ap.add_argument("--random-per-round", type=int, default=2000, help="每轮随机采样数(默认 2000)")
    ap.add_argument("--lake-start", default="2025-01-01")
    ap.add_argument("--out", default=None, help="输出路径(默认 tasks/sobol-<n>.json)")
    args = ap.parse_args()

    out = args.out or f"tasks/sobol-{args.n}.json"

    print(f"=== 循环采样:目标 {args.n} 唯一组(每轮 Sobol {args.sobol_per_round} + 随机 {args.random_per_round})===")
    params_list = collect_params(args.n, args.seed, args.sobol_per_round, args.random_per_round)
    print(f"\n采样完成:{len(params_list)} 唯一组")

    print(f"\n=== 导出 task.json(freeze 取 universe + 算 trial_id/三件哈希)===")
    task = export_task(params_list, lake_start=args.lake_start, seed=args.seed, out_path=out)

    print(f"\n[OK] 导出 task_id={task.task_id} -> {out}")
    print(f"   {len(task.trials)} 组 | git={task.git_commit[:8]} engine={task.engine_hash}")
    print(f"   parquet={task.parquet_sha256[:8]}... snapshot={task.snapshot_meta.snapshot_hash[:8]}")
    print(f"   universe={task.snapshot_meta.universe_count} date_range={task.snapshot_meta.date_range}")
    # 算力估算
    hours = len(task.trials) * 720 / 8 / 3600
    print(f"   算力估算:Mac 8 进程 × 单组 720s ≈ {hours:.1f} 小时 ≈ {hours/24:.1f} 天")
    print(f"\nMac 端跑批:")
    print(f"   git pull && git lfs pull")
    print(f"   python -m compute_unit verify {out}")
    print(f"   python -m compute_unit run {out} -o result.json")
    print(f"   python -m compute_unit summary result.json --top 5")


if __name__ == "__main__":
    main()
