# -*- coding: utf-8 -*-
"""组装器：内核逐字块 + §0 快照 + pilot_body → 单文件（C2 白名单变换-only）。

物理定位：
    掘金托管环境只收一个策略文件——本组装器把「仓库识别内核（逐字）+ 参数定稿
    快照（§0）+ 执行编排躯干（§2-§7）」拼成 emquant/emquant_neckline_pilot.py。
    拼接是纯函数（读五路输入→write_text，无时间戳/无随机），同输入产出逐字节
    一致（幂等红线，tests/emquant/test_kernel_equivalence.py::test_build_idempotent
    钉死）；生成物是【产物不是源】——任何修改改 pilot_body.py/内核/快照后重跑本
    脚本，手改生成物会被「重跑 build == 已提交产物」断言当场拦下。

变换纪律（C2）：
    ① 删 method_v0 的 `from .signal import Signal`（Signal 已内嵌于前段 signal.py 块）；
    ② `from __future__ import annotations` 从两个内核源提升到组装文件顶部
       （语法要求：future import 只许出现在模块头部）。
    其余任何字节差异都是事故——test_kernel_byte_equivalent 用归一化（两侧各删这两行后）
    substring 比对钉死。快照只注入 id_params/exec_params/trade_cfg/fingerprint 四者
    （notes/sources 是审计留档，不进单文件）。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUTURE = "from __future__ import annotations"
SIGNAL_IMPORT = "from .signal import Signal"

# 双腿账户常量（2026-08-28 双轨方案 §4.1）：C1 产物级账户锁的编译层——每腿产物编译
# 各自的 PILOT_ACCOUNT_ID，误交叉部署（实验产物粘主目录/反之）在 C1 启动期即拒。
# DEFAULT_EXP_ACCOUNT_ID 与 ops/gm_ops_common 同名常量互为镜像（emquant/ops 两家族
# 不互相 import；漂移由 C1 守卫兜底——产物绑错账户=启动拒绝，不是静默跑错腿）。
MAIN_ACCOUNT_ID = '67334fef-a137-11f1-8228-52560acd7da0'
DEFAULT_EXP_ACCOUNT_ID = 'c4ba3b2e-a2da-11f1-9262-52560acd7da0'

# amihud keep-top 信号过滤规格（2026-08-29 v3 用户裁决 keep_top=5）：当日信号
# > keep_top 时按 amihud60 降序留前 keep_top（高=最不流动=质量侧），其余剔；
# ≤ keep_top 或有效值不足 → 全保留豁免。验证=universe 子集四闸全过（剔 65%，
# 外层 Δ+17.8pp/全期 Δ+2.3pp/五年逐年全正）+ 史前窗（+0.78/+0.41 全正）；
# kt=4 差 0.09pp 触线、分位族/全市场线/板内线均否决，勿回退。策略自含
# （fetch_amihud60 现算）；识别参数指纹 7fe3d5b3 零变化。
AMIHUD_FILTER_CFG = {'enabled': True, 'window': 60, 'min_days': 48,
                     'keep_top': 5}

# 均衡栈守卫缺省（p_b221cbf5 · 2026-09-07）：主/exp 腿全关=零回归；
# --balanced 腿开启（T1 大盘节流 + skip@3.0 追价守卫）。数字依据见
# docs/research/2026-09-06-retrial-committee.md 附录（四方面对比）。
MARKET_THROTTLE_CFG_OFF = {'enabled': False, 'index': '399006.SZ', 'ma_window': 60}
MARKET_THROTTLE_CFG_ON = {'enabled': True, 'index': '399006.SZ', 'ma_window': 60}

# pilot_body 顶部「入口抑制块」的剪切标记（Task 8）：标记行本身随块一起搬进 head 区。
# Why 存在：内核逐字块（§1）尾部有 method_v0 的 `if __name__ == "__main__": main()`
# 演示守卫，位于产物 §2-§7 拼接位【之前】——抑制代码必须先于它执行才能拦住，而能落在
# §1 之前的只有 head 区（§0 前后）。块内容详注见 pilot_body.py 的 hoist 标记块内注释。
HOIST_BEGIN = "# [pilot-hoist:begin]"
HOIST_END = "# [pilot-hoist:end]"


def _strip(src: str, *drops: str) -> str:
    """删掉整行 strip 后与 drops 任一完全相等的行（C2 白名单逆变换，不做子串匹配）。"""
    return "\n".join(l for l in src.splitlines() if l.strip() not in drops).strip("\n")


def _hoist_entrance_guard(body: str) -> tuple[str, str]:
    """把 pilot_body 顶部的入口抑制标记块【剪切】出（head_block, body_rest）二元组。

    Why 剪切而非复制：块内 `_IS_MAIN = (__name__ == "__main__")` 若在产物出现两次，
    第二次（body 原位）会在脚本模式下把 _IS_MAIN 重算为 False（此时 __name__ 已被
    首次执行改写为 "pilot_kernel_suppressed"）→ 尾部 `if _IS_MAIN: run_pilot()` 永不
    触发——入口哑火。唯一一份、置于 head 区（§0 之前、§1 之前），才能既抑制内核
    演示块又保住尾部入口（tests/emquant/test_events_orchestration.py::
    test_entrance_suppression_hoisted_before_kernel_guard 钉死）。

    Why 标记缺失即 raise 而非容错跳过：入口机制是 Task 8 的交付红线之一，静默退回
    「脚本模式必炸」的旧态等于把事故藏进组装器——fail-loud 逼着改 pilot_body 的人
    与本函数同步评审。
    """
    pre, found, rest = body.partition(HOIST_BEGIN)
    if not found:
        raise RuntimeError("pilot_body.py 缺入口抑制块起始标记（# [pilot-hoist:begin]）——"
                           "Task 8 入口机制依赖，见 pilot_body.py 顶部 hoist 块头注")
    block, found_end, post = rest.partition(HOIST_END)
    if not found_end:
        raise RuntimeError("pilot_body.py 缺入口抑制块结束标记（# [pilot-hoist:end]）")
    return HOIST_BEGIN + block + HOIST_END, pre + post


def _build_stamp() -> str:
    """产物版本锚：git HEAD 提交时间（精确到秒）+ 短 hash。

    Why git 锚而非构建时钟（now()）：构建时钟破坏「同输入逐字节一致」幂等红线
    （test_build_idempotent 重跑即红）；git HEAD 时间在同一次提交内恒定——同提交
    重跑产物逐字节稳定，提交前进 stamp 前进，「掘金终端粘的是否最新版」用
    Ctrl+F 搜 PILOT_BUILD_STAMP 一眼可判（2026-08-25 部署现场需求：旧版重复
    粘贴导致同一崩溃复现两次的实锤教训）。
    为什么限定五路输入路径而非全仓 HEAD：别的模块提交会推进 HEAD 而产物未变，
    全仓锚会让「stamp 落后于 HEAD」的误报（部署核对口径=emquant 产物的最新
    提交，非全仓最新提交）。config/ 用**文件级锚**（params_snapshot/universe
    两文件而非目录——2026-08-29 教训：ab_round.json 等运营档案若落 config/
    会以目录粒度推 stamp,产物被迫随非输入文件重建）。git 不可用（异常）→
    "unknown"（fail-visible 不 fail-loud：组装本身仍可完成，stamp 缺失在部署
    核对时自然暴露）。
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%ci %h", "--",
             "emquant/pilot_body.py", "emquant/build_pilot.py",
             "emquant/config/params_snapshot.json", "emquant/config/universe.json",
             "strategies/neckline/signal.py",
             "strategies/neckline/method_v0.py"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build(output_path: Path | None = None, account_id: str = MAIN_ACCOUNT_ID,
          leg_suffix: str = "", balanced: bool = False) -> Path:
    """读五路输入 → 拼单文件 → 返回产物路径（幂等纯拼接）。

    双腿形态（2026-08-28 双轨方案 §4.1）：account_id 注入 §0 的 PILOT_ACCOUNT_ID
    （缺省=主账户，主产物字节不变——回归红线）；leg_suffix 追加进 stamp（exp 腿用
    " [exp]"，晨检看 INIT 行即辨腿，杜绝「比较对象错版本」）。实验产物走
    emquant_neckline_pilot_exp.py，与主产物同纪律入库（幂等断言+部署核对+评审 diff）。

    输入：strategies/neckline/{signal,method_v0}.py（逐字内核）、emquant/config/
    {params_snapshot,universe}.json（§0 定稿数据）、emquant/pilot_body.py（§2-§7）。
    段序（Why——识别先于执行，§0 常量供后段直接引用；入口抑制块必须先于 §1 见
    _hoist_entrance_guard 头注）：
        head（docstring+future）→ 入口抑制块（pilot_body 顶部剪出）→ §0 快照字面量+
        硬闸常量 → §1 内核逐字块 → §2-§7 body（剪出后的余量）。
    """
    snap = json.loads((ROOT / "emquant/config/params_snapshot.json").read_text(encoding="utf-8"))
    uni = json.loads((ROOT / "emquant/config/universe.json").read_text(encoding="utf-8"))
    # 内核逐字块：仅剥白名单两行（signal.py 无相对 import 行，只剥 future）
    sig = _strip((ROOT / "strategies/neckline/signal.py").read_text(encoding="utf-8"), FUTURE)
    mv0 = _strip((ROOT / "strategies/neckline/method_v0.py").read_text(encoding="utf-8"), FUTURE, SIGNAL_IMPORT)
    body_full = (ROOT / "emquant/pilot_body.py").read_text(encoding="utf-8")
    hoist, body = _hoist_entrance_guard(body_full)   # 入口抑制块剪出到 head 区（§0/§1 之前）
    stamp = _build_stamp() + leg_suffix
    _mt_cfg = MARKET_THROTTLE_CFG_ON if balanced else MARKET_THROTTLE_CFG_OFF
    _chase_skip = 3.0 if balanced else None
    head = (
        "# -*- coding: utf-8 -*-\n"
        '"""东财掘金·颈线策略单文件试点（组装产物，勿手改——改 pilot_body.py 后重跑 build_pilot.py）。\n'
        f"PARAMS_FINGERPRINT={snap['fingerprint']}  生成物见 emquant/config/。\n"
        f"PILOT_BUILD_STAMP: {stamp}（部署核对：编辑器 Ctrl+F 搜本串，与仓库 git log -1 比对）。\n"
        '"""\n' + FUTURE + "\n\n"
    )
    sec0 = (
        "# ============================ §0 参数区（export_snapshot 导出的定稿快照）============================\n"
        f"ID_PARAMS = {snap['id_params']!r}\n"
        f"EXEC_PARAMS = {snap['exec_params']!r}\n"
        f"TRADE_CFG = {snap['trade_cfg']!r}\n"
        f"UNIVERSE = {uni['symbols']!r}\n"
        f"PARAMS_FINGERPRINT = {snap['fingerprint']!r}\n"
        f"PILOT_BUILD_STAMP = {stamp!r}   # 版本锚（git HEAD 提交时间 + 短 hash）——部署核对用\n"
        "# 硬闸（2026-08-29 用户裁决：单日新挂 5，与 amihud keep_top=5 对齐——留 5 即挂 5；\n"
        "# 5 并发组合口径重验通过：基线外层中位 +28.5%，过滤臂五年逐年全正）：单票市值\n"
        "# ≤7.5%（与快照 pos_cap 同值，二次核验闸随动）；账户固定。\n"
        "PILOT_MAX_NEW_ORDERS_PER_DAY = 5\n"
        "PILOT_MAX_POSITION_PCT = 0.075\n"
        f"AMIHUD_FILTER = {AMIHUD_FILTER_CFG!r}\n"
        f"PILOT_ACCOUNT_ID = {account_id!r}\n"
        "# ── 均衡栈守卫（p_b221cbf5 · 2026-09-07 委员会重审判产物）──────────────────\n"
        "# ① MARKET_THROTTLE（T1 大盘节流）：创业板指收盘<MA60 → 跳过挂单段与死单回补\n"
        "#    （只拦增量，存量管理照跑，语义同 RISK_BLOCK；fail-open=数据失败不拦+WARN）。\n"
        "#    依据：六年实盘口径 dd -27.5→-10.6%/calmar 0.81→1.82（docs/research/\n"
        "#    2026-09-06-retrial-committee.md）。\n"
        "# ② CHASE_SKIP_ATR（skip@3.0 追价守卫）：chase 追入现价>颈线+N×ATR → 撤旧弃追\n"
        "#    （阈值平台 2.75-3.5，预注册取 3.0）。\n"
        "# 主/exp 腿默认全关（零回归）；--balanced 产出的均衡栈腿开启。\n"
        f"MARKET_THROTTLE = {_mt_cfg!r}\n"
        f"CHASE_SKIP_ATR = {_chase_skip!r}\n\n\n"
    )
    parts = [head, hoist + "\n\n", sec0,
             "# ============================ §1 识别内核（signal.py + method_v0.py 逐字块，C2）============================\n" + sig + "\n\n\n" + mv0 + "\n\n\n",
             "# ============================ §2-§7 执行编排（pilot_body.py）============================\n" + body]
    out = output_path or (ROOT / "emquant" / "emquant_neckline_pilot.py")
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def _cli() -> int:
    """CLI：--leg main|exp。exp 缺省产物=_exp.py、账户=env GM_EXP_ACCOUNT_ID 或镜像常量。"""
    import argparse
    import os
    ap = argparse.ArgumentParser(description="组装单文件产物（主/实验腿）")
    ap.add_argument("--leg", choices=["main", "exp", "balanced"], default="main")
    ap.add_argument("--account-id", help="覆盖 §0 PILOT_ACCOUNT_ID（缺省按腿）")
    ap.add_argument("--out", help="输出路径（缺省按腿）")
    args = ap.parse_args()
    if args.leg == "main":
        account = args.account_id or MAIN_ACCOUNT_ID
        out = Path(args.out) if args.out else None
        suffix = ""
    elif args.leg == "balanced":
        account = args.account_id or ""
        out = Path(args.out) if args.out else (ROOT / "emquant" / "emquant_neckline_pilot_balanced.py")
        suffix = " [balanced]"
    else:
        account = args.account_id or os.environ.get("GM_EXP_ACCOUNT_ID") or DEFAULT_EXP_ACCOUNT_ID
        out = Path(args.out) if args.out else (ROOT / "emquant" / "emquant_neckline_pilot_exp.py")
        suffix = " [exp]"
    print(build(out, account_id=account, leg_suffix=suffix,
                balanced=(args.leg == "balanced")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
