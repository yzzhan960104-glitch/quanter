# -*- coding: utf-8 -*-
"""§3 状态层 + §4 风控闸（组装产物口径）：state.pkl 原子写/双值文件/试点三硬闸。

物理定位：
    被测对象是【组装产物】emquant/emquant_neckline_pilot.py 而非源 pilot_body.py——
    改源后必须先重跑 emquant/build_pilot.py 再跑本文件（与 test_kernel_equivalence
    同范式）。import 范式照抄：先注册 sys.modules 再 exec_module——§1 内核 Signal
    是 dataclass + PEP563 字符串注解，dataclasses 解析注解要回查
    sys.modules[cls.__module__]，不注册则 exec 当场 AttributeError(NoneType)。

隔离纪律（Why——仓库 emquant/state|audit/ 是运行时真值区，测试写入即污染）：
    一切落盘（state/审计/CAP/flag）全部指向 tmp_path——优先走函数的显式 path 参数
    （§3 接口为测试与编排显式留了可选路径参数），模块常量兜底场景（如 read_cap
    内嵌的审计 WARN）用 monkeypatch 模块属性：函数体在【调用时】才查模块常量，
    patch 即生效、teardown 即还原，配合每用例全新 exec 产物做到零残留。

口径锚点（断言期望值全部由此推出，非拍脑袋）：
    CAP 扣减式 pre_open.py:540：额度 = 总权益×CAP − 持仓市值 − 未终态买单占额；
    恰等余量放行（严格 > 才拦）pre_open.py:602 `_order_amt > _pos_quota`；
    未终态买单计入敞口 state_store.py:1470 get_open_buy_amount；
    CAP 值非法视同 0 全拦 state_store.py:2016 resolve_risk_control（I-4）；
    block 只拦增量、存量管理照常 pre_open.py:478（ADR-16）。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"
TODAY = "2026-08-21"          # 与 placed/scan_done 同格式的交易日字符串（schema v1 口径）


def _import_artifact():
    """按文件位置 exec 组装产物为全新模块（范式与理由见模块 docstring）。"""
    spec = importlib.util.spec_from_file_location("emquant_pilot_state_gates", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m          # 先注册再 exec：dataclass 注解回查依赖
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def pilot():
    """每用例全新 exec 一次产物——上一用例对模块常量的 monkeypatch/属性改动零残留。"""
    return _import_artifact()


def _fresh_state(pilot, placed_today=()):
    """构造只填当日 placed 的空白态（check_caps 各闸用例的公共起点）。

    placed 预填当日单号 = 模拟「今日已挂 N 单」的盘初态，其余键保持 schema v1 空白。
    """
    st = pilot._initial_state()
    st["placed"][TODAY] = list(placed_today)
    return st


# ============================================================================
# §3 save_state / load_state：tmp+rename 原子写与 schema v1 回转
# ============================================================================
def test_save_state_atomic(tmp_path, pilot, monkeypatch):
    """replace 中途失败（断电/目标被占用）时：旧 state 完好、无 tmp 残留。

    Why 这是 §3 的头号红线：state 记着 orders/positions——半写坏文件 = 持仓记忆
    丢失 = 裸奔。原子写承诺「目标文件要么旧要么新，永不半新半旧」。
    """
    state_file = tmp_path / "state.pkl"
    old = pilot._initial_state()
    old["scan_done"].add(TODAY)
    old["orders"]["id_old"] = {"symbol": "600000.SH", "status": "PENDING", "qty": 100}
    pilot.save_state(old, path=state_file)
    before = state_file.read_text(encoding="utf-8")          # 旧内容快照（字节级对照）

    def _boom(src, dst):                                     # 模拟 os.replace 阶段崩溃
        raise OSError("模拟 replace 失败（断电/文件被占用）")

    monkeypatch.setattr(os, "replace", _boom)                # 模块内 os.replace 调用点即时可见
    new = pilot._initial_state()
    new["orders"]["id_new"] = {"symbol": "000001.SZ", "status": "PENDING", "qty": 200}
    with pytest.raises(OSError):
        pilot.save_state(new, path=state_file)               # 崩溃必须上抛（静默吞 = 假成功落盘）

    assert state_file.read_text(encoding="utf-8") == before  # 原文件字节未动
    back = pilot.load_state(path=state_file)                 # 旧内容仍可完整读回
    assert back["orders"] == old["orders"]
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"replace 失败后残留 tmp（污染晨检目录）：{leftovers}"


def test_load_state_missing_returns_init(tmp_path, pilot):
    """缺文件返 schema v1 空白态（首启语义）——scan_done 必须是 set（成员测/防重语义）。"""
    st = pilot.load_state(path=tmp_path / "不存在.pkl")
    assert st == {"version": 1, "scan_done": set(), "placed": {}, "orders": {}, "positions": {}}
    assert isinstance(st["scan_done"], set)


def test_state_roundtrip_set_and_nested(tmp_path, pilot):
    """save→load 全键往返：set↔list 转换、嵌套 dict 原样、调用方 state 不被改形。"""
    st = pilot._initial_state()
    st["scan_done"].update({"2026-08-20", TODAY})
    st["placed"][TODAY] = ["id1", "id2"]
    st["orders"]["id1"] = {"symbol": "600000.SH", "price": 10.5, "qty": 100,
                           "exec_params": {"max_wait": 8, "trailing_step": 0.0}}
    st["positions"]["600000.SH"] = {"entry_date": TODAY, "qty": 100, "remaining_qty": 100,
                                    "trailing": {"activated": False, "cur_stop": 9.9}}
    p = tmp_path / "state.pkl"
    pilot.save_state(st, path=p)
    back = pilot.load_state(path=p)
    assert back["scan_done"] == {"2026-08-20", TODAY} and isinstance(back["scan_done"], set)
    assert back["placed"] == st["placed"]                    # dict[str, list[str]] 原样
    assert back["orders"] == st["orders"]                    # 含 exec_params 嵌套 dict
    assert back["positions"] == st["positions"]              # 含 trailing 嵌套 dict
    # 盘上必须是 JSON 文本（人工可检视/跨版本修复），set 落盘为排序 list（产出确定性）
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(raw["scan_done"], list)
    assert raw["scan_done"] == ["2026-08-20", TODAY]         # 排序落盘：同状态两次落盘字节一致
    # 调用方手里的 state 不被 save 改形（编排层还要继续用它跑当轮）
    assert isinstance(st["scan_done"], set)


def test_load_state_corrupt_fails_loud(tmp_path, pilot):
    """损坏文件 fail-loud 抛错，绝不静默回空白态。

    Why：原子写已消灭「写到一半崩溃」这一唯一常态损坏源，文件仍坏 = 磁盘损坏或
    人工误编辑——静默重置会把 orders/positions 一并抹掉 = 持仓裸奔无止损管理。
    """
    p = tmp_path / "state.pkl"
    p.write_text("{不是合法JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        pilot.load_state(path=p)


def test_load_state_version_mismatch_rejected(tmp_path, pilot):
    """version != 1 拒载：schema 冻结 v1，未来升版必须显式迁移，绝不静默猜结构。"""
    p = tmp_path / "state.pkl"
    p.write_text(json.dumps({"version": 2, "scan_done": [], "placed": {},
                             "orders": {}, "positions": {}}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        pilot.load_state(path=p)
    p.write_text(json.dumps({"version": 1, "placed": {}, "orders": {}, "positions": {}}),
                 encoding="utf-8")                            # 缺 scan_done 键 = 不完整 v1
    with pytest.raises(RuntimeError):
        pilot.load_state(path=p)


# ============================================================================
# §3 人工风控双值文件：RISK_BLOCK.flag / CAP.txt
# ============================================================================
def test_block_flag_blocks_via_check_gates(tmp_path, pilot):
    """is_blocked 纯存在性语义：flag 在 = 拦增量，flag 无 = 放行；与内容无关。

    （block 与额度闸无关——block 在编排层拦「扫描后挂单前」，本用例只钉 is_blocked
    语义本身：人工 touch/rm 是回退 SOP 第一步，任何内容解析都会给出错多留一步。）
    """
    flag = tmp_path / "RISK_BLOCK.flag"
    assert pilot.is_blocked(path=flag) is False              # 无 flag：默认放行
    flag.write_text("", encoding="utf-8")                     # 人工 touch（内容空亦拦）
    assert pilot.is_blocked(path=flag) is True
    flag.write_text("随便什么内容", encoding="utf-8")          # 内容不参与语义
    assert pilot.is_blocked(path=flag) is True
    flag.unlink()                                             # 人工 rm：解除拦截
    assert pilot.is_blocked(path=flag) is False


def test_read_cap_branches(tmp_path, pilot, monkeypatch):
    """CAP.txt 四分支：缺→1.0（不限制）/ 合法→原值 / 非数字→0.0 全拦 / 越界·NaN→0.0 全拦。

    刻意不对称（与本地 state_store.py:2016 同口径）：缺文件是正常态（CAP 是可选
    收紧项），值坏是事故态（人工想收紧却写错时最坏组合是静默放行）→ fail-closed。
    """
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path / "audit")   # WARN 留痕落 tmp 不进仓库
    cap_file = tmp_path / "CAP.txt"
    assert pilot.read_cap(path=cap_file) == 1.0               # 缺文件 → 缺省不限制
    cap_file.write_text("0.5", encoding="utf-8")
    assert pilot.read_cap(path=cap_file) == 0.5               # 合法数值原样
    cap_file.write_text(" 0.25 \n", encoding="utf-8")         # 容忍首尾空白/换行（人工编辑常态）
    assert pilot.read_cap(path=cap_file) == 0.25
    cap_file.write_text("abc", encoding="utf-8")
    assert pilot.read_cap(path=cap_file) == 0.0               # 非数字 → 全拦
    cap_file.write_text("50", encoding="utf-8")               # 把 50% 写成 50 的人工典型错
    assert pilot.read_cap(path=cap_file) == 0.0               # 越界 [0,1] → 全拦
    cap_file.write_text("-0.1", encoding="utf-8")
    assert pilot.read_cap(path=cap_file) == 0.0
    cap_file.write_text("nan", encoding="utf-8")
    assert pilot.read_cap(path=cap_file) == 0.0               # NaN 防渗透（链式比较判 False）
    # 两类 WARN 都进了当日 audit CSV（晨检能发现「以为设了 0.5 其实文件没了」的静默失效）
    audit_csv = (tmp_path / "audit" / f"audit_{date.today():%Y%m%d}.csv")
    rows = audit_csv.read_text(encoding="utf-8").splitlines()
    assert sum(1 for r in rows if "cap_missing" in r) == 1
    assert sum(1 for r in rows if "cap_invalid" in r) >= 1


def test_audit_log_appends_csv(tmp_path, pilot):
    """audit_log 三列追加（ts,event,detail-JSON）：§7 审计的物理写入原语先落位钉死。"""
    pilot.audit_log("SIGNAL", audit_dir=tmp_path, symbol="600000.SH", neckline=100.5)
    pilot.audit_log("WARN", audit_dir=tmp_path, msg="测试行")
    f = tmp_path / f"audit_{date.today():%Y%m%d}.csv"
    rows = list(csv.reader(f.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 2 and all(len(r) == 3 for r in rows)  # 列结构恒定三列
    assert rows[0][1] == "SIGNAL"
    assert json.loads(rows[0][2])["symbol"] == "600000.SH"    # detail 是 JSON 字段包
    assert rows[1][1] == "WARN"


# ============================================================================
# §4 check_caps：试点三硬闸（CAP 逐单扣减 / 单日 ≤2 / 单票 ≤5%）
# ============================================================================
def test_cap_quota_deduction(pilot, monkeypatch):
    """CAP 逐单扣减：额度 = equity×CAP − 持仓 − 已挂（pre_open.py:540 同式）。

    大金额单会先撞 5% 单票闸——本用例隔离 CAP 闸：把试点单票上限临时放宽到 50%。
    brief 数值对（equity=1M、CAP=0.5、持仓 200k、已挂 100k → 余量 200k）下
    「150k 拒 / 100k 过」只在逐单扣减【之后】成立（已挂累进到 200k → 余量 100k），
    两个相位都钉死。
    """
    monkeypatch.setattr(pilot, "PILOT_MAX_POSITION_PCT", 0.5)
    st = _fresh_state(pilot)
    kw = dict(sod_state=st, equity=1_000_000, positions_mv=200_000,
              open_buy_amount=100_000, today=TODAY, cap=0.5)   # 余量 = 500k−200k−100k = 200k
    ok, why = pilot.check_caps(price=10.0, qty=10_000, **kw)    # 本单 100k < 200k → 过
    assert ok and why == ""
    ok, _ = pilot.check_caps(price=10.0, qty=20_000, **kw)      # 本单 200k 恰等余量 → 过
    assert ok                                                    # 严格 > 才拦（pre_open.py:602 同口径）
    ok, why = pilot.check_caps(price=10.0, qty=25_000, **kw)     # 本单 250k > 200k → 拒
    assert not ok and "额度" in why
    # 逐单扣减（调用方循环侧累进，对齐 pre_open 循环语义）：挂出 100k 后已挂=200k
    # → 余量收紧到 100k，brief 的「150k 拒 / 100k 过」数值对在此相位成立
    kw2 = dict(kw, open_buy_amount=200_000)
    ok, why = pilot.check_caps(price=10.0, qty=15_000, **kw2)    # 150k > 100k → 拒
    assert not ok and "额度" in why
    ok, _ = pilot.check_caps(price=10.0, qty=10_000, **kw2)      # 100k 恰等收紧后余量 → 过
    assert ok


def test_cap_fail_closed(pilot):
    """equity/持仓/挂额任一查询失败（编排层显式传 None）→ 拒——不知道占多少就盲放是最坏的。"""
    st = _fresh_state(pilot)
    ok, why = pilot.check_caps(st, None, 0.0, 0.0, price=10.0, qty=100, today=TODAY, cap=0.5)
    assert not ok and "fail-closed" in why                      # equity 查询失败（get_cash 静默返空的显式化）
    ok, why = pilot.check_caps(st, None, 0.0, 0.0, 10.0, 100, TODAY, cap=1.0)
    assert not ok                                               # CAP=1.0 默认态同样拒（单票闸也依赖 equity）
    ok, _ = pilot.check_caps(st, 1_000_000, None, 0.0, 10.0, 100, TODAY, cap=0.5)
    assert not ok                                               # 持仓市值查不到
    ok, _ = pilot.check_caps(st, 1_000_000, 0.0, None, 10.0, 100, TODAY, cap=0.5)
    assert not ok                                               # 未终态买单占额查不到（state_store.py:1470 同义输入）
    ok, _ = pilot.check_caps(st, 0.0, 0.0, 0.0, 10.0, 100, TODAY, cap=0.5)
    assert not ok                                               # equity 拿到脏 0 值（≤0）同拒
    ok, why = pilot.check_caps(st, 1_000_000, 0.0, 0.0, price=0.0, qty=100, today=TODAY, cap=1.0)
    assert not ok and "委托参数" in why                          # price×qty ≤ 0 的残缺委托同属「不知道就拦」


def test_daily_order_cap(pilot):
    """单日新挂 ≤2（FR3 试点硬闸）：当日 placed 已 2 → 拒；只 1 → 过。其余闸全绿隔离本闸。"""
    st = _fresh_state(pilot, placed_today=["id1", "id2"])       # 当日已挂 2 单
    ok, why = pilot.check_caps(st, 1_000_000, 0.0, 0.0, price=10.0, qty=4_000, today=TODAY, cap=1.0)
    assert not ok and "单日" in why
    st2 = _fresh_state(pilot, placed_today=["id1"])
    ok, _ = pilot.check_caps(st2, 1_000_000, 0.0, 0.0, 10.0, 4_000, TODAY, cap=1.0)
    assert ok                                                   # 4 万 < 5%×1M 单票闸不干扰


def test_symbol_cap(pilot):
    """单票金额 ≤5%×equity（FR3）：60k > 50k 拒、恰等 50k 过（严格 >）。"""
    st = _fresh_state(pilot)
    ok, why = pilot.check_caps(st, 1_000_000, 0.0, 0.0, price=10.0, qty=6_000, today=TODAY, cap=1.0)
    assert not ok and "单票" in why
    ok, _ = pilot.check_caps(st, 1_000_000, 0.0, 0.0, 10.0, 5_000, TODAY, cap=1.0)
    assert ok


def test_check_caps_reads_cap_file_by_default(pilot, monkeypatch, tmp_path):
    """cap 缺省时现场读 CAP.txt（read_cap 内嵌 WARN 审计）——文件值真实生效。"""
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path)           # 缺文件分支的 WARN 落 tmp
    monkeypatch.setattr(pilot, "CAP_FILE", tmp_path / "CAP.txt")
    st = _fresh_state(pilot)
    ok, _ = pilot.check_caps(st, 1_000_000, 0.0, 0.0, price=10.0, qty=1_000, today=TODAY)
    assert ok                                                   # 无 CAP.txt → 1.0：额度=1M 放行
    (tmp_path / "CAP.txt").write_text("0.00001", encoding="utf-8")
    ok, why = pilot.check_caps(st, 1_000_000, 0.0, 0.0, 10.0, 1_000, TODAY)
    assert not ok and "额度" in why                             # 额度=10 → 1 万单被拒：证明确实读了文件
