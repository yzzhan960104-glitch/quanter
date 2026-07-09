# -*- coding: utf-8 -*-
"""caisen.storage 计划持久化 + 形态失败冷却黑名单（Phase 3 · Task 1）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线 Phase 3 的"计划状态仓储层"——把 TradePlanGenerator
    产出的候选计划落 JSON 文件，管理其状态机迁移（PENDING_APPROVAL → APPROVED →
    ARMED → FILLED → CLOSED），并维护形态失败冷却黑名单（假突破标的冷却）。

    纯 JSON 文件读写，无 DB 依赖（CLAUDE.md 拒绝过度设计 + 彻底掌控执行环境）：
        plans/<date>.json   T 日候选计划列表（save_plans 落盘，含 status 字段）
        plans/active.json   ARMED/FILLED 活跃计划索引（执行器/持仓监控高频读路径）
        plans/cooldown.json 形态失败黑名单 {symbol: expire_date}，过期自动失效

蔡森方法学对齐：
    形态失败冷却 = 蔡森实战"假突破标的冷却"机制的工程化——避免执行器在已确认
    失效的形态上反复消耗（流动性枯竭 + 假突破连环亏损的双重防御）。

序列化契约（与 caisen/__main__.py _write_plans_json 对齐）：
    - plans/<date>.json 结构：{"date": str, "plans": [{plan1}, ...]}
      每个 plan dict 含 TradePlan 全字段 + status（状态机当前态）。
    - pd.Timestamp 字段（formed_at/valid_until/max_holding_until）序列化为
      ISO 字符串（JSON 原生支持），反序列化用 pd.Timestamp(str) 还原。
    - save_plans 默认每个 plan 写入 status="PENDING_APPROVAL"（审核待批态）。

防御性边界（CLAUDE.md 量化风控·边界审查）：
    - plans/ 目录 lazy 创建（os.makedirs exist_ok=True），首存自动建目录；
    - 文件读写全程 UTF-8 + ensure_ascii=False（中文 metadata 不乱码）；
    - update_plan 不存在 plan_id 抛 KeyError（状态机不进 NULL，防消息乱序脏数据）；
    - in_cooldown 用 ISO 日期字符串字典序比较（"YYYY-MM-DD" 字典序 = 时间序），
      until_date 当日仍冷却、次日释放（保守冷却，避免边界日误放行）；
    - load_plans / load_active_plans 文件缺失时返回空列表（不抛异常，幂等读）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import Optional

import pandas as pd

from caisen.plan import TradePlan


# ---------------------------------------------------------------------------
# 模块级常量（便于测试 monkeypatch 隔离）
# ---------------------------------------------------------------------------
# plans 目录（相对工作目录；lazy 创建）。测试通过 monkeypatch.setattr 覆盖。
_PLANS_DIR = "plans"

# active.json / cooldown.json 固定文件名（不随日期变化，是跨日聚合索引）。
_ACTIVE_FILE = "active.json"
_COOLDOWN_FILE = "cooldown.json"

# TradePlan 中 pd.Timestamp 类型字段的集合（序列化 ISO 字符串 ↔ 反序列化 Timestamp）。
# 来源：caisen/plan.py TradePlan dataclass 字段定义（formed_at/valid_until/max_holding_until）。
_TS_FIELDS = ("formed_at", "valid_until", "max_holding_until")

# 初始状态：save_plans 落盘的每个计划默认 status（蔡森流水线要求人工审核后才推进）。
_DEFAULT_STATUS = "PENDING_APPROVAL"

# 活跃状态集合：进入这两个状态的计划同步写入 active.json（执行器高频读路径）。
_ACTIVE_STATUSES = ("ARMED", "FILLED")

# ISO 日期严格正则：仅 YYYY-MM-DD（4位年-2位月-2位日）。
# Why 严格：save_plans 的 date 直接拼进文件名 plans/<date>.json，任何非标准字符
# （含 "../" 路径跳板、"/"/"\\" 分隔符、空格等）都可能造成路径遍历或注入（B-2）。
# 正则先做格式拦截，_validate_iso_date 内再用 pd.Timestamp 做语义二次校验（如月份 13）。
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# 序列化辅助：TradePlan ↔ dict（Timestamp ISO 互转）
# ---------------------------------------------------------------------------
def _plan_to_dict(plan: TradePlan, status: str = _DEFAULT_STATUS) -> dict:
    """TradePlan → 可 JSON 序列化的 dict（Timestamp 转 ISO 字符串）。

    用 dataclasses.asdict 递归转 dict，再对 pd.Timestamp 字段单独 isoformat。
    metadata dict 内若含 Timestamp 不处理（当前契约无此场景；防御性留给调用方）。
    """
    d = asdict(plan)
    for k in _TS_FIELDS:
        v = d.get(k)
        if v is not None:
            d[k] = pd.Timestamp(v).isoformat()
    d["status"] = status
    return d


def _restore_plan_dict(d: dict) -> dict:
    """dict（JSON 反序列化后）→ Timestamp 字段还原为 pd.Timestamp。

    load_plans / get_plan / load_active_plans 读盘后统一走这里，保证下游拿到的
    formed_at/valid_until/max_holding_until 是 pd.Timestamp（而非裸字符串），
    与 TradePlanGenerator 内存态类型一致。
    """
    for k in _TS_FIELDS:
        v = d.get(k)
        if v is not None and not isinstance(v, pd.Timestamp):
            d[k] = pd.Timestamp(v)
    return d


# ---------------------------------------------------------------------------
# 文件 I/O 辅助（UTF-8 + 幂等读）
# ---------------------------------------------------------------------------
def _full_path(name: str) -> str:
    """拼接 _PLANS_DIR 下的完整路径（不创建目录）。"""
    return os.path.join(_PLANS_DIR, name)


def _ensure_dir() -> None:
    """lazy 创建 plans/ 目录（exist_ok=True，多进程/多次调用幂等）。"""
    os.makedirs(_PLANS_DIR, exist_ok=True)


def _validate_iso_date(date: str) -> None:
    """校验 date 为严格 YYYY-MM-DD，非法抛 ValueError（B-2 路径遍历/注入防御）。

    两道防线：
        1. 正则 _ISO_DATE_RE：格式拦截，拒绝含 "../" / "\\" / "/" / 空格等任何
           非标准字符的输入——这是防路径遍历的核心（date 直接拼进文件名）；
        2. pd.Timestamp 二次解析：拦截 re 通过但语义非法的输入（如 "2024-13-01"
           月份越界），pd.Timestamp 对非法日期抛异常。

    Why 不静默容错：date 是安全敏感输入（决定文件落盘路径），任何非法值都必须
    显式失败而非猜测纠正——显式优于隐式（CLAUDE.md 量化风控·边界审查）。
    """
    if not isinstance(date, str) or not _ISO_DATE_RE.match(date):
        raise ValueError(f"非法日期（须 YYYY-MM-DD）：{date!r}")
    try:
        pd.Timestamp(date)   # 语义二次校验（月份/日期越界）
    except Exception as exc:
        raise ValueError(f"非法日期（解析失败）：{date!r}") from exc


def _read_json(path: str, default):
    """读 JSON 文件，文件缺失/JSON 异常时返回 default（幂等读，不抛异常）。

    防御性（CLAUDE.md 量化风控·边界审查）：plans 文件可能被人工误删/写入损坏，
    读路径必须降级而非崩溃（执行器读盘失败应返回空，不应让整个流水线宕机）。
    """
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 文件损坏/权限异常 → 降级返回 default，记录可由调用方上层处理
        return default


def _write_json(path: str, data) -> None:
    """写 JSON 文件（UTF-8 + ensure_ascii=False，中文 metadata 不转义）。"""
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 公开接口：save_plans / load_plans / get_plan / update_plan
# ---------------------------------------------------------------------------
def save_plans(date: str, plans: list) -> None:
    """把 TradePlan 列表落 plans/<date>.json（T 日候选计划持久化）。

    参数：
        date:  交易日字符串（如 "2024-06-01"），作为文件名。
        plans: TradePlan 列表（dataclass 实例，用 asdict 序列化）。

    落盘结构（与 caisen/__main__.py _write_plans_json 兼容）：
        {"date": "2024-06-01", "plans": [{plan1 with status, plan2, ...}]}
    每个计划默认 status=_DEFAULT_STATUS（PENDING_APPROVAL）。

    幂等性：同 date 重复 save 会整体覆盖（T 日重跑筛形态时是期望行为）。
    """
    # date 安全校验（B-2）：拼文件名前强制 YYYY-MM-DD，防路径遍历/注入。
    # 先于 _ensure_dir：非法输入不应产生任何副作用（连目录都不建）。
    _validate_iso_date(date)
    _ensure_dir()
    plans_serial = [_plan_to_dict(p, status=_DEFAULT_STATUS) for p in plans]
    payload = {"date": str(date), "plans": plans_serial}
    _write_json(_full_path(f"{date}.json"), payload)


def _load_raw_plans() -> list[dict]:
    """扫描 plans/ 下所有 <date>.json（排除 active/cooldown 索引文件），合并 plans 列表。

    日期文件名约定：YYYY-MM-DD.json（或任意非 active/cooldown 的 .json）。
    合并顺序按文件名字典序（≈ 日期序），保证 load_plans 返回顺序稳定可预测。
    """
    if not os.path.isdir(_PLANS_DIR):
        return []
    excluded = {_ACTIVE_FILE, _COOLDOWN_FILE}
    plan_files = sorted(
        f for f in os.listdir(_PLANS_DIR)
        if f.endswith(".json") and f not in excluded
    )

    all_plans: list[dict] = []
    for fname in plan_files:
        payload = _read_json(_full_path(fname), {"plans": []})
        # 兼容两种结构：包裹 {"date":..., "plans":[...]} 与裸 [...]
        if isinstance(payload, dict):
            plans_list = payload.get("plans", [])
        elif isinstance(payload, list):
            plans_list = payload
        else:
            plans_list = []
        all_plans.extend(plans_list)
    return all_plans


def load_plans(status: Optional[str] = None) -> list[dict]:
    """跨日期合并加载所有候选计划，可选按 status 过滤。

    参数：
        status: 状态过滤（None=不过滤返回全部；"APPROVED"/"ARMED"/... 过滤）。

    返回：
        list[dict]，每个 dict 是 _restore_plan_dict 处理后的 plan（Timestamp 已还原）。
        无 plans 文件时返回空列表（不抛异常）。

    用途：
        - 审核器/CLI 低频全量浏览（扫所有日期）；
        - 实盘执行器高频读活跃计划请用 load_active_plans（O(1) 定位 ARMED/FILLED）。
    """
    plans_raw = _load_raw_plans()
    restored = [_restore_plan_dict(dict(d)) for d in plans_raw]
    if status is not None:
        restored = [d for d in restored if d.get("status") == status]
    return restored


def get_plan(plan_id: str) -> Optional[dict]:
    """按 plan_id 精确查询单个计划（跨所有日期文件扫描）。

    命中返回 _restore_plan_dict 处理后的 dict；未命中返回 None。
    O(N) 扫描（N=所有 plans 文件总计划数），适合低频查询；高频场景调用方应缓存。
    """
    for d in _load_raw_plans():
        if d.get("plan_id") == plan_id:
            return _restore_plan_dict(dict(d))
    return None


def _find_plan_file(plan_id: str) -> Optional[str]:
    """定位 plan_id 所在的 <date>.json 完整路径（update_plan 内部用）。"""
    if not os.path.isdir(_PLANS_DIR):
        return None
    excluded = {_ACTIVE_FILE, _COOLDOWN_FILE}
    for fname in sorted(os.listdir(_PLANS_DIR)):
        if not fname.endswith(".json") or fname in excluded:
            continue
        payload = _read_json(_full_path(fname), {"plans": []})
        plans_list = payload.get("plans", []) if isinstance(payload, dict) else []
        if any(d.get("plan_id") == plan_id for d in plans_list):
            return _full_path(fname)
    return None


def update_plan(plan_id: str, **fields) -> None:
    """更新指定计划的字段（状态迁移 + 任意字段增量）。

    参数：
        plan_id: 计划唯一标识（save_plans 时由 TradePlan.plan_id 写入）。
        **fields: 要更新的字段键值对（如 status="ARMED", fill_price=10.05）。
                  新字段（原 plan 不存在）会被增量添加（执行器回填成交数据场景）。

    异常：
        KeyError: plan_id 不存在（防御性：状态机不进 NULL，防消息乱序脏数据）。

    副作用（状态机同步）：
        - 更新原 <date>.json 中该 plan 的字段；
        - 若新 status ∈ {ARMED, FILLED}：同步写入/更新 active.json；
        - 若新 status == CLOSED：从 active.json 移除（持仓已了结）。
    """
    # 1. 定位 plan 所在文件
    plan_file = _find_plan_file(plan_id)
    if plan_file is None:
        raise KeyError(f"update_plan: plan_id={plan_id!r} 不存在于任何 plans/<date>.json")

    # 2. 更新原文件中的 plan 字段
    payload = _read_json(plan_file, {"plans": []})
    plans_list = payload.get("plans", []) if isinstance(payload, dict) else []
    target = None
    for d in plans_list:
        if d.get("plan_id") == plan_id:
            target = d
            break
    if target is None:
        # 文件级竞态：_find_plan_file 命中但此处找不到，按 KeyError 处理（防御）
        raise KeyError(f"update_plan: plan_id={plan_id!r} 定位后丢失（文件竞态）")
    target.update(fields)
    _write_json(plan_file, payload)

    # 3. 状态机同步 active.json（ARMED/FILLED 进、CLOSED 出）
    new_status = fields.get("status")
    if new_status in _ACTIVE_STATUSES:
        _sync_to_active(target)
    elif new_status == "CLOSED":
        _remove_from_active(plan_id)


# ---------------------------------------------------------------------------
# active.json 管理（ARMED/FILLED 活跃计划索引）
# ---------------------------------------------------------------------------
def _sync_to_active(plan_dict: dict) -> None:
    """把 plan_dict 同步进 active.json（存在则更新，不存在则追加）。

    用途：update_plan 推进到 ARMED/FILLED 时调用，保证 active.json 始终反映
    当前"待执行/持仓中"的计划全集。
    """
    active = _read_json(_full_path(_ACTIVE_FILE), {"plans": []})
    plans_list = active.get("plans", []) if isinstance(active, dict) else []
    plan_id = plan_dict.get("plan_id")
    # 存在则原地更新，不存在则追加
    for d in plans_list:
        if d.get("plan_id") == plan_id:
            d.update(plan_dict)
            break
    else:
        plans_list.append(dict(plan_dict))
    active["plans"] = plans_list
    _write_json(_full_path(_ACTIVE_FILE), active)


def _remove_from_active(plan_id: str) -> None:
    """从 active.json 移除指定 plan_id（CLOSED 时调用）。"""
    active = _read_json(_full_path(_ACTIVE_FILE), {"plans": []})
    plans_list = active.get("plans", []) if isinstance(active, dict) else []
    plans_list = [d for d in plans_list if d.get("plan_id") != plan_id]
    active["plans"] = plans_list
    _write_json(_full_path(_ACTIVE_FILE), active)


def load_active_plans() -> list[dict]:
    """加载 active.json 中的活跃计划（ARMED/FILLED），Timestamp 字段还原。

    文件缺失时返回空列表（不抛异常）。执行器/持仓监控高频读路径。
    """
    active = _read_json(_full_path(_ACTIVE_FILE), {"plans": []})
    plans_list = active.get("plans", []) if isinstance(active, dict) else []
    return [_restore_plan_dict(dict(d)) for d in plans_list]


# ---------------------------------------------------------------------------
# 冷却黑名单（形态失败标的冷却）
# ---------------------------------------------------------------------------
def add_to_cooldown(symbol: str, until_date: str) -> None:
    """把标的加入形态失败冷却黑名单，直到 until_date（含当日）。

    参数：
        symbol:     标的代码（如 "FAKE.SZ"）。
        until_date: 冷却截止日 ISO 字符串（如 "2024-06-10"），当日仍冷却、次日释放。

    语义：同标的多日 add 取最新 until_date（覆盖，用于延长冷却期）。
    持久化：落 cooldown.json，跨进程（screen/execute）共享。
    """
    cooldown = _read_json(_full_path(_COOLDOWN_FILE), {})
    if not isinstance(cooldown, dict):
        cooldown = {}
    cooldown[symbol] = str(until_date)
    _write_json(_full_path(_COOLDOWN_FILE), cooldown)


def in_cooldown(symbol: str, date: str) -> bool:
    """查询标的在指定日期是否处于冷却期。

    返回：
        True  : symbol 在黑名单中，且 date <= until_date（含 until_date 当日）；
        False : symbol 不在黑名单，或 date > until_date（已过期，自动失效）。

    实现说明：用 ISO 日期字符串字典序比较（"YYYY-MM-DD" 字典序 = 时间序），
    避免 strptime 解析开销与异常面。要求 date 与 until_date 同为合法 ISO 日期。
    """
    cooldown = _read_json(_full_path(_COOLDOWN_FILE), {})
    if not isinstance(cooldown, dict):
        return False
    until = cooldown.get(symbol)
    if until is None:
        return False
    # ISO 日期字典序 = 时间序：date <= until → 命中（含 until 当日）
    return str(date) <= until
