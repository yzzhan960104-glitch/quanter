# -*- coding: utf-8 -*-
"""终端腿运行时每日归档器（哑终端原则的最轻备份步 · 2026-08-22）。

物理意图：
    掘金腿的 state/audit 全部落在终端策略目录（~/.emgm3/projects/<strategy_id>/），
    单机单副本——重装终端/误删策略目录即丢档案。在途单与持仓可由柜台对账
    （absorb_reality）重建，但历史 orders/positions 档案、cooldown 锚
    （last_signal）与 audit 原始件不可再生——双轨 20 交易日的比对证据就躺在
    这一个目录里。本脚本把「跑批时点的运行时快照」拷回仓库侧 emquant/archive/，
    解掉证据单点，同时为 W3「audit → trading_state.db 采集器」备好原料
    （采集方向性预告：掘金腿继续产 CSV 哑终端，仓库侧单向消费——本工具是
    该方向的第一节车厢，故掘金腿单文件与终端侧一字不动）。

归档内容（源目录内有什么收什么，缺失跳过不报错）：
    audit/audit_*.csv     全部历史审计件（首跑全量收，之后每日增量新文件）
    state/state.pkl       策略唯一持久状态快照（JSON 内容，覆盖式取最新）
    state/RISK_BLOCK.flag 人工风控开关（0 字节也拷——存在性即拦截语义，归档里
                          能复盘「当日增量是否被拦」）
    state/CAP.txt         人工仓位上限值（同上，人工风控面的另一半）
    manifest.json         本工具生成：源目录绝对路径/发现方式/拷贝时刻/逐文件
                          size+sha256——归档可追溯到策略目录，哈希供完整性核对。

产物落位：emquant/archive/<leg>/<YYYY-MM-DD>/（腿×跑批日一目录，2026-08-28 双腿化）——同日重跑覆盖当日目录
    （拿到的是更晚时点的快照，幂等），跨日互不干扰，20 日双轨自然形成时间序列。
    归档目录 gitignore（运行时产物不入库，同 state/audit 治理）。

时机建议：15:35 盘后跑最优（after_close 落完 EOD 行与终态 state）；盘中可跑，
    audit 是追加写，拷贝瞬间可能截尾最后一行——csv 半行人工可辨不致命
    （audit_log 头注本就按「崩溃留半行」设计），但归档以盘后为准。

用法（Git Bash、仓库根目录）：
    PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/archive_terminal_state.py
    # 双腿形态（2026-08-28 双轨 §4.3）：按 ops/gm_ops_common.LEGS 注册表逐腿归档，
    # 落 emquant/archive/<leg>/<YYYY-MM-DD>/（main 恒在；exp 部署后 env 启用自动跟进）。
    # 显式归档任意目录（单腿回看/异位目录）：
    ... archive_terminal_state.py --src "<策略目录>" --leg main

退出码：0=成功（全部在役腿）/ 2=无在役腿 / 3=--src 目录无效 / 4=拷贝过程异常。
依赖：stdlib + ops.gm_ops_common（腿注册表单源；其 dotenv 为 ImportError 容错可选，
.venv310/.venv_emquant 双端零差异，parquet 级依赖禁入）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ops import gm_ops_common as gc   # noqa: E402 腿注册表单源（sys.path 注入后）
# 归档收清单：源相对路径 → 语义（manifest 的 files 键沿用相对路径，检索直观）
_RUNTIME_FILES = ("state/state.pkl", "state/RISK_BLOCK.flag", "state/CAP.txt")
_AUDIT_GLOB = "audit/audit_*.csv"


def _has_runtime_data(d: Path) -> bool:
    """判定策略目录是否跑出过运行时数据（audit csv 或 state.pkl 任一在场）。

    只看这两类：config/runtime.json 是人工输入不是策略产物（裸挂载未首启的目录
    也可能有），main.py 所有策略目录都有——它们不构成「这条腿真跑过」的证据。
    """
    if (d / "state" / "state.pkl").exists():
        return True
    return bool(list(d.glob(_AUDIT_GLOB)))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _archive_one(src: Path, dest_root: Path, leg_key: str, kind: str) -> int:
    """单腿归档（audit 全量 + 三件运行时文件 → dest_root/<leg>/<今日>/）。"""
    if not _has_runtime_data(src):
        print(f"[archive] 跳过（无运行时数据——未首启/新部署腿）：{src}")
        return 0
    files: list[Path] = sorted(src.glob(_AUDIT_GLOB))
    files += [src / rel for rel in _RUNTIME_FILES if (src / rel).exists()]
    if not files:
        print(f"[archive] 终止：源目录运行时文件均不在场（形态异变，人工核查）：{src}")
        return 3
    day_dir = dest_root / leg_key / f"{date.today():%Y-%m-%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"leg": leg_key, "archived_at": datetime.now().isoformat(timespec="seconds"),
                "source_dir": str(src.resolve()), "source_kind": kind, "files": {}}
    try:
        for f in files:
            target = day_dir / f.name        # 平铺（audit 文件名自带日期，state 同名覆盖即最新）
            shutil.copy2(f, target)
            manifest["files"][f.name] = {"size": f.stat().st_size, "sha256": _sha256(f)}
    except OSError as e:
        print(f"[archive] 终止：拷贝异常（源被锁/磁盘满？）：{type(e).__name__}: {e}")
        return 4
    (day_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[archive][{leg_key}] 源（{kind}）：{src}")
    print(f"[archive][{leg_key}] 归档：{day_dir}（{len(manifest['files'])} 件）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="掘金终端腿运行时归档（仓库侧单向采集，哑终端零改动）")
    ap.add_argument("--src", help="显式归档任意策略目录（缺省=按腿注册表逐腿）")
    ap.add_argument("--leg", default="main", choices=[l.key for l in gc.LEGS],
                    help="--src 时归入的腿子目录（默认 main）")
    ap.add_argument("--dest", default=str(ROOT / "emquant" / "archive"),
                    help="归档根目录（默认 emquant/archive/）")
    args = ap.parse_args()

    dest_root = Path(args.dest)
    if args.src:
        src = Path(args.src)
        if not src.is_dir():
            print(f"[archive] 终止：--src 目录不存在：{src}")
            return 3
        rc = _archive_one(src, dest_root, args.leg, "explicit")
        return rc
    # 注册表主路径：逐在役腿归档（腿发现单源=ops/gm_ops_common.active_legs）
    legs = gc.active_legs()
    if not legs:
        print("[archive] 终止：无在役腿（main 恒应在——env GM_STRATEGY_DIR 指向不存在目录？）")
        return 2
    worst = 0
    for leg in legs:
        d = gc.leg_strategy_dir(leg)
        if d is None:
            continue
        worst = max(worst, _archive_one(d, dest_root, leg.key, "registry"))
    return worst


if __name__ == "__main__":
    sys.exit(main())
