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


def _strip(src: str, *drops: str) -> str:
    """删掉整行 strip 后与 drops 任一完全相等的行（C2 白名单逆变换，不做子串匹配）。"""
    return "\n".join(l for l in src.splitlines() if l.strip() not in drops).strip("\n")


def build(output_path: Path | None = None) -> Path:
    """读五路输入 → 拼单文件 → 返回产物路径（幂等纯拼接）。

    输入：strategies/neckline/{signal,method_v0}.py（逐字内核）、emquant/config/
    {params_snapshot,universe}.json（§0 定稿数据）、emquant/pilot_body.py（§2-§7）。
    段序（Why——识别先于执行，§0 常量供后段直接引用）：
        head（docstring+future）→ §0 快照字面量+硬闸常量 → §1 内核逐字块 → §2-§7 body。
    """
    snap = json.loads((ROOT / "emquant/config/params_snapshot.json").read_text(encoding="utf-8"))
    uni = json.loads((ROOT / "emquant/config/universe.json").read_text(encoding="utf-8"))
    # 内核逐字块：仅剥白名单两行（signal.py 无相对 import 行，只剥 future）
    sig = _strip((ROOT / "strategies/neckline/signal.py").read_text(encoding="utf-8"), FUTURE)
    mv0 = _strip((ROOT / "strategies/neckline/method_v0.py").read_text(encoding="utf-8"), FUTURE, SIGNAL_IMPORT)
    body = (ROOT / "emquant/pilot_body.py").read_text(encoding="utf-8")
    head = (
        "# -*- coding: utf-8 -*-\n"
        '"""东财掘金·颈线策略单文件试点（组装产物，勿手改——改 pilot_body.py 后重跑 build_pilot.py）。\n'
        f"PARAMS_FINGERPRINT={snap['fingerprint']}  生成物见 emquant/config/。\n"
        '"""\n' + FUTURE + "\n\n"
    )
    sec0 = (
        "# ============================ §0 参数区（export_snapshot 导出的定稿快照）============================\n"
        f"ID_PARAMS = {snap['id_params']!r}\n"
        f"EXEC_PARAMS = {snap['exec_params']!r}\n"
        f"TRADE_CFG = {snap['trade_cfg']!r}\n"
        f"UNIVERSE = {uni['symbols']!r}\n"
        f"PARAMS_FINGERPRINT = {snap['fingerprint']!r}\n"
        "# 试点硬闸（spec FR3）：单日新挂 ≤2；单票市值 ≤5%；仿真账户固定。\n"
        "PILOT_MAX_NEW_ORDERS_PER_DAY = 2\n"
        "PILOT_MAX_POSITION_PCT = 0.05\n"
        "PILOT_ACCOUNT_ID = 'e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1'\n\n\n"
    )
    parts = [head, sec0,
             "# ============================ §1 识别内核（signal.py + method_v0.py 逐字块，C2）============================\n" + sig + "\n\n\n" + mv0 + "\n\n\n",
             "# ============================ §2-§7 执行编排（pilot_body.py）============================\n" + body]
    out = output_path or (ROOT / "emquant" / "emquant_neckline_pilot.py")
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(build())
