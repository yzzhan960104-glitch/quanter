# -*- coding: utf-8 -*-
"""caisen.replay_runs 回放结果持久化（方案 A：默认全存 + 支持删除）。

（待迁·Step4 移出 caisen 包至执行编排层）本模块当前物理位于 caisen/infra/ 过渡子包，
Step4 将连同 storage/execution/backtest_replay/replay_*/viz_* 整体迁出 caisen 包至独立的
执行编排层。当前位置仅为 Step3 分层重构的中间态。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森「历史回放」结果的仓储层——把每次 run_replay 产出的
    ReplayReportResponse + 触发它的 ReplayRequest 落 JSON 文件，供前端浏览历史 /
    加载对比 / 手动删除。

    方案 A（用户决策）：相同配置+标的的重复回放【不去重】，每次都存。理由：
    backtest_replay.replay 是纯确定性的（dict 插入序迭代 + 按 exit_date 排序 +
    纯 sum/count 统计，无 random/Set 乱序），相同输入→相同结果，去重理论上不丢信息；
    但保留全部以审计/对比，由前端删除按钮做清理，而非写入时哈希去重（避免去重逻辑
    漏判维度、且保留多次运行的"同配置不同时刻"时间线）。

    纯 JSON 文件读写，无 DB 依赖（与 caisen/storage.py 同源范式）：
        replay_runs/<run_id>.json   单次完整结果（request + summary + report）
        replay_runs/index.json      轻量摘要列表（list 端点 O(N) 读小记录，不读 trades）
        replay_runs/.lock           跨进程字节锁（串行化 index 的读-改-写，防并发丢更新）

    run_id 格式：YYYYMMDD-HHMMSS-<6hex>（时间序前缀 + uuid 短码防同秒碰撞）。
    此格式同时是白名单正则——get_run/delete_run 仅放行匹配的 run_id 拼文件名，
    杜绝 "../" 路径遍历（同 storage._validate_iso_date 的防御思想，B-2 同源）。

防御性边界（CLAUDE.md 量化风控·边界审查）：
    - replay_runs/ 目录 lazy 创建（os.makedirs exist_ok=True），首存自动建目录；
    - <run_id>.json / index.json 全程 UTF-8 + ensure_ascii=False（中文建议不乱码）；
    - 原子写（tempfile + os.replace，B-14 同源）：读路径永不撞见半截文件；
    - 跨进程字节锁串行化 index 的 RMW（replay 与删除并发不丢更新）；
    - run_id 白名单正则：非法 id（含路径跳板/空串）→ get_run 返 None / delete_run 返 False，
      不抛异常、不读写目录外文件；
    - list_runs 文件缺失/损坏 → 返空列表（幂等读，不抛，同 storage.load_plans 契约）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# 模块级常量（便于测试 monkeypatch 隔离，与 storage.py 同源范式）
# ---------------------------------------------------------------------------
# replay_runs 目录（相对工作目录；lazy 创建）。测试通过 monkeypatch.setattr 覆盖。
_REPLAY_RUNS_DIR = "replay_runs"

# index.json 固定文件名（跨 run 聚合的摘要索引，list 端点读它而非逐文件读）。
_INDEX_FILE = "index.json"

# run_id 严格白名单：YYYYMMDD-HHMMSS-<6hex>。
# Why 严格：run_id 直接拼进文件名 replay_runs/<run_id>.json，任何非标准字符
# （含 "../" 路径跳板、"/"/"\\" 分隔符、空格等）都可能造成路径遍历或注入。
# get_run/delete_run 仅放行此格式，非法 id 静默拒绝（返 None/False，不抛）。
_RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


# ---------------------------------------------------------------------------
# run_id 生成（可 monkeypatch，保证测试顺序断言确定性）
# ---------------------------------------------------------------------------
def _gen_run_id() -> tuple[str, str]:
    """生成 (run_id, created_at_iso)，两者同源同一时刻。

    run_id = YYYYMMDD-HHMMSS-<6hex>：时间序前缀（list 排序/人眼可读）+ uuid 短码
    （防同秒多次回放碰撞——确定性回放下同秒两次 save 也产生不同 id）。
    created_at = isoformat(timespec="microseconds")：list 降序排序键 + 前端展示时间。

    Why 微秒精度（非秒）：created_at 是 list_runs 降序排序键，秒级精度下同秒多次
    回放（前端快速连点 / 测试连发两次）会得到相同 key，稳定排序保留原序 → 「最新
    在前」失效。微秒精度让同秒内的先后也能正确排序（生产 + 测试同源正确）。
    前端展示时可自行截断到秒，存储层保留全精度仅供排序。

    Why 抽成独立函数：测试 monkeypatch 它返回递增的确定性序列，保证 list_runs
    顺序断言不受真实时间/uuid 随机性影响（同 storage._PLANS_DIR 的可测性手法）。
    """
    now = datetime.now()
    created_at = now.isoformat(timespec="microseconds")
    run_id = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return run_id, created_at


# ---------------------------------------------------------------------------
# 文件 I/O 辅助（UTF-8 + 原子写 + 幂等读；与 storage.py 同源，自包含不复用其私有符号）
# ---------------------------------------------------------------------------
def _full_path(name: str) -> str:
    """拼接 _REPLAY_RUNS_DIR 下的完整路径（不创建目录）。"""
    return os.path.join(_REPLAY_RUNS_DIR, name)


def _ensure_dir() -> None:
    """lazy 创建 replay_runs/ 目录（exist_ok=True，多进程/多次调用幂等）。"""
    os.makedirs(_REPLAY_RUNS_DIR, exist_ok=True)


def _read_json(path: str, default):
    """读 JSON 文件，文件缺失/JSON 异常时返回 default（幂等读，不抛异常）。

    防御性：replay_runs 文件可能被人工误删/写入损坏，读路径必须降级而非崩溃
    （list 端点读 index 失败应返空列表，不应让回放历史浏览宕机）。
    """
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: str, data) -> None:
    """原子写 JSON（tempfile + os.replace，B-14 同源）：读路径永不撞见半截文件。

    Why 原子：直接 open(w) 写到一半若进程崩溃/断电，会留下残缺 JSON。改为先写同目录
    临时文件再 os.replace 原子替换（同文件系统下原子）——要么旧要么新，无中间态。
    """
    _ensure_dir()
    # 临时文件放同目录：os.replace 仅在同文件系统下原子（跨目录可能非原子）。
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        # 写/替换失败：清理临时文件，保持原文件不动（原子语义）。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _runs_write_lock():
    """跨进程独占锁（串行化 index 的读-改-写，防并发丢更新，B-14 同源）。

    Why 锁：run_replay（写）与 delete_run（删）可能并发对 index.json 做
    read-modify-write，无锁会丢更新（A、B 都读到旧 index，后写覆盖先写）。
    原子写只保证单次写完整，不防 RMW 竞态——故用 sentinel 文件字节锁串行化整段 RMW。

    实现：锁 replay_runs/.lock 文件首字节。Win 用 msvcrt.locking(LK_LOCK 阻塞直到获得)，
    POSIX 用 fcntl.flock(LOCK_EX)；fd 关闭即释放（进程崩溃亦自动释放，无死锁残留）。
    """
    _ensure_dir()
    lock_path = _full_path(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if sys.platform == "win32":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
        else:  # pragma: no cover - POSIX 路径，当前部署以 Win 为主
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _validate_run_id(run_id: str) -> bool:
    """校验 run_id 严格匹配白名单正则（B-2 路径遍历防御，与 storage._validate_iso_date 同源）。

    返回 True/False 而非抛异常：get_run/delete_run 对非法 id 静默拒绝（返 None/False），
    与「run_id 不存在」语义合并为同一种「无此记录」结果，调用方无需区分。
    """
    return isinstance(run_id, str) and bool(_RUN_ID_RE.match(run_id))


# ---------------------------------------------------------------------------
# 摘要提取（list 端点的轻量视图，不含完整 trades/equity_curve）
# ---------------------------------------------------------------------------
def _summary(run_id: str, created_at: str, request: dict, report: dict) -> dict:
    """从 request + report 提取列表视图摘要字段。

    物理意图：list 端点只需展示「何时跑的 / 什么参数 / 关键统计」——不含完整 trades
    与 equity_curve（这些可能是数百笔的大列表），保持 index.json 轻量、list O(N) 读快。
    完整记录由 get_run 读 <run_id>.json 提供。

    universe_n 语义：None（全市场）记 -1（前端显示「全市场」），列表记 len。
    cfg_min_rr：从 cfg_override 取 min_rr_ratio（最关键的调参，列表快览；缺省则 None）。
    """
    universe = request.get("universe")
    universe_n = -1 if universe is None else len(universe)
    cfg = request.get("cfg_override") or {}
    return {
        "run_id": run_id,
        "created_at": created_at,
        "start": request.get("start"),
        "end": request.get("end"),
        "universe_n": universe_n,
        "cfg_min_rr": cfg.get("min_rr_ratio"),
        "n_hits": report.get("n_hits", 0),
        "win_rate": report.get("win_rate", 0.0),
        "avg_rr": report.get("avg_rr", 0.0),
        "max_drawdown": report.get("max_drawdown", 0.0),
        "annualized_return": report.get("annualized_return", 0.0),
    }


# ---------------------------------------------------------------------------
# 公开接口：save_run / list_runs / get_run / delete_run
# ---------------------------------------------------------------------------
def save_run(request: dict, report: dict) -> str:
    """落盘单次回放结果，返回新生成的 run_id。

    参数：
        request: ReplayRequest.model_dump() 等价 dict（start/end/universe/cfg_override/save）。
        report:  ReplayReportResponse.model_dump() 等价 dict（含 trades/equity_curve 全字段）。

    落盘结构：
        replay_runs/<run_id>.json = {run_id, created_at, request, summary, report}
        replay_runs/index.json    = [summary, ...]（追加本次 summary）

    方案 A（用户决策）：不去重——相同 request 的重复 save 产生新 run_id，保留全部。
    防御性：整段「写 run 文件 + 更新 index」在 _runs_write_lock 内，防并发丢更新。
    """
    run_id, created_at = _gen_run_id()
    summary = _summary(run_id, created_at, request, report)
    run_payload = {
        "run_id": run_id,
        "created_at": created_at,
        "request": request,
        "summary": summary,
        "report": report,
    }
    # 写锁串行化「写 run 文件→读旧 index→追加→写新 index」整段 RMW（B-14 同源）。
    with _runs_write_lock():
        _write_json(_full_path(f"{run_id}.json"), run_payload)
        # index RMW：读旧（缺省 []）→ 追加 → 原子写新。
        index = _read_json(_full_path(_INDEX_FILE), [])
        if not isinstance(index, list):
            index = []   # index 损坏降级为空（不抛，本次仍写回干净 index）
        index.append(summary)
        _write_json(_full_path(_INDEX_FILE), index)
    return run_id


def list_runs() -> list[dict]:
    """读 index.json 摘要列表，按 created_at 降序返回（最新在前）。

    返回：list[summary dict]。无 index 文件/目录时返回空列表（不抛异常）。
    不含完整 trades/equity_curve（保持轻量；完整记录用 get_run）。
    """
    index = _read_json(_full_path(_INDEX_FILE), [])
    if not isinstance(index, list):
        return []
    # 降序：最新运行在前（前端历史列表直觉顺序）。created_at 为 ISO 字符串，
    # 字典序与时间序一致（同 storage.in_cooldown 的 ISO 字典序比较手法）。
    return sorted(
        (s for s in index if isinstance(s, dict)),
        key=lambda s: str(s.get("created_at", "")),
        reverse=True,
    )


def get_run(run_id: str) -> Optional[dict]:
    """读 replay_runs/<run_id>.json 完整记录（request + summary + report）。

    返回：完整 dict；run_id 不存在或非法（路径遍历）→ None（路由层转 404）。
    """
    if not _validate_run_id(run_id):
        # 非法 run_id（含路径跳板/空串）静默拒绝，不拼路径不读文件（B-2 同源防御）。
        return None
    payload = _read_json(_full_path(f"{run_id}.json"), None)
    if not isinstance(payload, dict):
        return None
    return payload


def delete_run(run_id: str) -> bool:
    """删除指定 run：移除 <run_id>.json + 从 index 移除其 summary。

    返回：True=删除成功；False=run_id 不存在或非法（不抛异常）。
    防御性：整段「删 run 文件→读旧 index→移除→写新 index」在锁内，防并发丢更新。
    """
    if not _validate_run_id(run_id):
        return False
    with _runs_write_lock():
        path = _full_path(f"{run_id}.json")
        if not os.path.isfile(path):
            return False   # 不存在 → False（与「非法 id」合并为「无此记录」语义）
        try:
            os.remove(path)
        except OSError:
            return False   # 删除失败（权限/竞态）→ False，不污染 index
        # index 同步移除：读旧→过滤→写新。
        index = _read_json(_full_path(_INDEX_FILE), [])
        if isinstance(index, list):
            index = [s for s in index
                     if not (isinstance(s, dict) and s.get("run_id") == run_id)]
            _write_json(_full_path(_INDEX_FILE), index)
        return True
