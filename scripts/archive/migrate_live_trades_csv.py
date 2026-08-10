# -*- coding: utf-8 -*-
"""一次性迁移：live_trades.csv 双文件分裂 → 单一真相源（2026-08-02）。

背景（病灶）：
    - presentation/server/http/config.py 的 PROJECT_ROOT 旧实现少上溯一级，
      LIVE_TRADE_LOG 落在 ``presentation/logs/live_trades.csv``；
    - 修复后服务统一读写仓库根 ``logs/live_trades.csv``（与引擎日志/文档同目录）。

本脚本做三步：
    1. 以 ``presentation/logs/live_trades.csv`` 为源（含 07-26 之后全部真实活动），
       剔除测试/冒烟污染行：
       - 510300.SH 100股@5.0（FakeGW 冒烟测试单，BUY/BLOCKED/DRY_RUN_BUY 全删）；
       - DRYRUN.SZ（测试专用 symbol）；
       - 300001.SZ / 300002.SZ / 600000.SH 的「成交回报@…」行（测试成交回报注入）。
    2. 归一化 schema：补上 kind 列（老行 7 字段 → 按 rationale 推断
       fill=成交回报 / submit=其余审计行），写仓库根 ``logs/live_trades.csv``。
    3. 归档旧文件（.bak 保留可恢复，绝不直接删除）：
       - logs/live_trades.csv            → 纯测试污染（439 行 510300/DRYRUN.SZ）
       - presentation/logs/live_trades.csv → 迁移源

运行：python scripts/archive/migrate_live_trades_csv.py
⚠️ 迁移后需重启 uvicorn / trading 进程（模块级 LIVE_TRADE_LOG 常量已求值）。
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

# **ssot-review P3 fix**：scripts/archive/ 子目录，parents[1] 解析到 E:\quanter\scripts
# （非仓库根）。改 parents[2]：archive → scripts → 仓库根。原 parents[1] 仅在脚本位于
# scripts/ 直下时成立，迁移到 archive/ 后路径失效（ROOT_CSV/PRES_CSV 全错）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_CSV = PROJECT_ROOT / "logs" / "live_trades.csv"
PRES_CSV = PROJECT_ROOT / "presentation" / "logs" / "live_trades.csv"

HEADER = ["timestamp", "symbol", "direction", "shares", "price", "strategy",
          "rationale", "kind"]

# 测试/冒烟污染判定（符号级指纹，宁可误删测试行也不让假成交进生产统计；
# 旧文件全程 .bak 归档，可恢复）。
SMOKE_SYMBOL_QTY_PRICE = ("510300.SH", 100.0, 5.0)
TEST_FILL_SYMBOLS = {"300001.SZ", "300002.SZ", "600000.SH"}


def _is_test_row(r: list[str]) -> bool:
    sym = r[1] if len(r) > 1 else ""
    direction = (r[2] if len(r) > 2 else "").upper()
    try:
        shares = float(r[3]) if len(r) > 3 and r[3] else 0.0
        price = float(r[4]) if len(r) > 4 and r[4] else 0.0
    except ValueError:
        shares, price = 0.0, 0.0
    rationale = r[6] if len(r) > 6 else ""
    if sym == "DRYRUN.SZ":
        return True
    if (sym, shares, price) == SMOKE_SYMBOL_QTY_PRICE and direction in (
            "BUY", "BLOCKED", "DRY_RUN_BUY", "DRY_RUN_SELL", "SELL"):
        return True
    if sym in TEST_FILL_SYMBOLS and rationale.startswith("成交回报@"):
        return True
    return False


def _normalize(r: list[str]) -> list[str]:
    """老行 7 字段 → 8 字段（kind 列）：成交回报=fill，其余审计行=submit。"""
    row = (r + [""] * len(HEADER))[: len(HEADER) - 1]
    kind = ""
    if len(r) >= len(HEADER) and r[7].strip():
        kind = r[7].strip()
    else:
        rationale = r[6] if len(r) > 6 else ""
        kind = "fill" if rationale.startswith("成交回报@") else "submit"
    return row + [kind]


def main() -> int:
    if not PRES_CSV.exists():
        print(f"迁移源不存在：{PRES_CSV}（可能已迁移过，跳过）")
        return 0

    with PRES_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过旧表头
        all_rows = [list(r) for r in reader if any(c.strip() for c in r)]

    kept = [r for r in all_rows if not _is_test_row(r)]
    dropped = len(all_rows) - len(kept)

    # 归档旧文件（可恢复），再写新单一真相源
    for src, suffix in ((ROOT_CSV, "test-polluted-20260726"),
                        (PRES_CSV, "merged-20260802")):
        bak = src.with_suffix(f".csv.bak-{suffix}")
        if src.exists() and not bak.exists():
            shutil.copy2(src, bak)
            print(f"归档 {src.name} → {bak.name}")

    ROOT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with ROOT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for r in kept:
            writer.writerow(_normalize(r))

    from collections import Counter
    by_sym = Counter(r[1] for r in kept)
    print(f"迁移完成：{len(kept)} 行保留 / {dropped} 行测试污染剔除 → {ROOT_CSV}")
    print("保留明细（按 symbol）：", dict(by_sym))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
