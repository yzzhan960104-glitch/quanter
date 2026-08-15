# -*- coding: utf-8 -*-
"""_drop_engine_descendants 递归祖先链去重单测（T10 实证关闭 · 2026-08-15）。

快照实证背景（只读 wmic/Get-CimInstance，2026-08-15 12:47，live 引擎运行中）：
    PID 1456  venv python -m trading（launcher，进程树根）
      └ PID 11548  系统 Python310 python -m trading（base 解释器，持 :8000 的 live 引擎）
          └ PID 28612  venv python spawn worker（cmdline=spawn_main，**不含** -m trading）

    → 嵌套父子真实发生（T4 根因 B 形态：venv 启动器拉起 base 解释器双 cmdline），
      但为**单条进程树**非两条独立树（真·双起抢同一 sid 未发生）。旧「直接父 ∈
      引擎集合」一级判据恰好覆盖两级链；本组测试把「经非匹配中间进程（spawn
      worker / cmd.exe）挂下来的孙辈」也钉死，并守住「两条独立树都保留」（探测
      绝不掩盖真双起）与「PID 复用环不死循环」两条边界。
"""
from __future__ import annotations

from ops.process_topology import _drop_engine_descendants


def _proc(pid: int, ppid: int) -> dict:
    """造一条 matched 形状的进程记录（pid/ppid 足够测去重，exe/cmdline 不参与）。"""
    return {"pid": pid, "ppid": ppid, "exe": None, "cmdline": f"-m trading#{pid}"}


def test_snapshot_shape_launcher_base_keeps_root_only():
    """回放 2026-08-15 实测快照：1456(launcher)→11548(base) 两级均匹配 → 只留根。

    一级 ppid 判据（旧实现）本就覆盖两级链——此测同时是旧语义的回归锚。
    """
    matched = [_proc(1456, 26716), _proc(11548, 1456)]
    ppid_of = {1456: 26716, 11548: 1456, 26716: 4}
    roots = _drop_engine_descendants(matched, ppid_of)
    assert [p["pid"] for p in roots] == [1456]


def test_grandchild_through_spawn_worker_is_dropped():
    """经 spawn worker（cmdline 不含 -m trading，不在 matched 但在全表）挂的孙辈被清。

    快照实证：11548(引擎)→28612(spawn worker)→假设 28612 再拉 -m trading 孙辈 X。
    旧一级判据：X 的直接父 28612 ∉ 引擎集合 → 漏网（audit 误计 2 引擎）。
    递归祖先链：X→28612→11548 ∈ 引擎集合 → 判后代丢弃。
    """
    matched = [_proc(1456, 26716), _proc(11548, 1456), _proc(91000, 28612)]
    ppid_of = {1456: 26716, 11548: 1456, 28612: 11548, 91000: 28612, 26716: 4}
    roots = _drop_engine_descendants(matched, ppid_of)
    assert [p["pid"] for p in roots] == [1456]


def test_descendant_through_non_python_intermediate_is_dropped():
    """经非 python 中间进程（cmd.exe/.bat 包装）挂下来的 -m trading 子进程被清。

    全表 ppid_of 含 cmd.exe（pid 700）——只取 python 子集会在中间进程处断链，
    这正是 engine_processes 改取全进程表建 pid→ppid 图的原因。
    """
    matched = [_proc(1456, 26716), _proc(92000, 700)]
    ppid_of = {1456: 26716, 700: 1456, 92000: 700}  # 700=cmd.exe（非 python 也在表）
    roots = _drop_engine_descendants(matched, ppid_of)
    assert [p["pid"] for p in roots] == [1456]


def test_two_independent_trees_both_kept():
    """两条独立树（真·双起抢同一 sid）都保留——探测不掩盖双起事实。

    物理意图：若去重误并两条独立树，audit「引擎数=1」假绿、stop 只树杀其一，
    残留进程继续抢 sid——比漏报更危险（T4 根因 A/B 的发现依赖这个计数）。
    """
    matched = [_proc(1456, 26716), _proc(11548, 1456), _proc(31000, 30000)]
    ppid_of = {1456: 26716, 11548: 1456, 31000: 30000, 30000: 1, 26716: 4}
    roots = _drop_engine_descendants(matched, ppid_of)
    assert sorted(p["pid"] for p in roots) == [1456, 31000]


def test_pid_reuse_cycle_terminates():
    """PID 复用造成的祖先环不死循环——seen 集切断回溯。

    环型一（环在**非引擎**祖先链上）：matched 引擎的 ppid 链走进 300↔400 环，
    回溯必须终止且**不误丢根**（环上无引擎 pid → 判非后代，根保留）。
    现实成因：父进程死后 pid 被回收，新进程恰好拿到旧 pid 作自己的父。
    """
    matched = [_proc(100, 300)]
    ppid_of = {100: 300, 300: 400, 400: 300}  # 300↔400 成环（均非引擎）
    roots = _drop_engine_descendants(matched, ppid_of)
    assert [p["pid"] for p in roots] == [100]


def test_pid_reuse_cycle_between_engines_degenerates_to_drop_both():
    """环型二（环落在**两个匹配引擎之间**）：互为祖先 → 双双判后代丢弃（空）。

    与旧一级判据行为一致（旧：ppid ∈ pids 双双命中同弃）——极端病理（PID 复用恰好
    落在两个 -m trading 匹配进程间）下宁可探测为空，交上层端口/pid 文件锚点兜底，
    也不能挂死探测（supervisor/audit 卡死比漏报更危险）。此测同时钉「不死循环」。
    """
    matched = [_proc(100, 200), _proc(200, 100)]  # 互为父子（环）
    ppid_of = {100: 200, 200: 100}
    roots = _drop_engine_descendants(matched, ppid_of)
    assert roots == []


def test_empty_matched_returns_empty():
    """空匹配集（CIM 正常但无引擎进程）→ 空列表（start 路径依赖此语义）。"""
    assert _drop_engine_descendants([], {1: 0}) == []
