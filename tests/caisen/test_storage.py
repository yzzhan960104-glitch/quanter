# -*- coding: utf-8 -*-
"""caisen.storage 计划持久化 + 形态失败冷却黑名单测试（Phase 3 · Task 1）。

物理意图与覆盖节点（CLAUDE.md 量化风控·边界审查）：
  本测试验证蔡森形态学流水线 Phase 3 的计划持久化内核——
    1. save_plans/load_plans 往返一致（含 pd.Timestamp → ISO 字符串 → 还原 Timestamp）；
    2. load_plans 按 status 过滤、跨日期合并（T 日 + T+1 日候选合并视图）；
    3. update_plan 状态迁移（PENDING_APPROVAL → APPROVED → ARMED）+ 任意字段更新；
    4. add_to_cooldown / in_cooldown 形态失败冷却黑名单（命中 + 过期自动失效）；
    5. ARMED/FILLED 活跃计划落 active.json（执行器/持仓监控的高频读路径）。

设计要点（CLAUDE.md 极简 + 显式原则）：
  - 全程用 tmp_path fixture 隔离测试（monkeypatch storage._PLANS_DIR），绝不污染真实 plans/；
  - Timestamp 序列化用 isoformat，反序列化用 pd.Timestamp(str) 还原（保持时区一致）；
  - cooldown 字典 {symbol: expire_date}，过期日期严格小于查询日才命中（expire 当日仍冷却）。

蔡森方法学对齐：
  形态失败冷却是蔡森实战中"假突破标的冷却"机制的工程化——
  避免执行器在已确认失效的形态上反复消耗（流动性枯竭 + 假突破连环亏损的双重防御）。
"""
import json
from dataclasses import replace

import pandas as pd
import pytest

from caisen import storage
from caisen.plan import TradePlan


# ---------------------------------------------------------------------------
# 测试辅助：合成 TradePlan + tmp_path 隔离
# ---------------------------------------------------------------------------
def _make_plan(
    *,
    plan_id: str = "p-0001",
    symbol: str = "000001.SZ",
    formed_at: pd.Timestamp = pd.Timestamp("2024-06-01"),
    valid_until: pd.Timestamp = pd.Timestamp("2024-06-05"),
    max_holding_until: pd.Timestamp = pd.Timestamp("2024-06-24"),
) -> TradePlan:
    """构造合成 TradePlan（frozen 值对象），字段值仅保证类型合法，数值无策略含义。

    用于 storage 往返测试——只关心序列化/反序列化保真，不关心盈亏比等业务校验。
    """
    return TradePlan(
        plan_id=plan_id,
        symbol=symbol,
        pattern_type="w_bottom",
        formed_at=formed_at,
        breakout_price=10.0,
        neckline_price=11.0,
        bottom_price=9.0,
        H=2.0,
        entry_upper=10.0,
        entry_lower=9.7,
        stop_loss=9.0,
        take_profit=13.0,
        take_profit_2x=15.0,
        rr_ratio=3.0,
        valid_until=valid_until,
        max_holding_until=max_holding_until,
        timeout_exit_threshold=0.01,
        shares=1000,
        metadata={"depth": 0.22, "note": "合成测试计划"},
    )


@pytest.fixture(autouse=True)
def _isolate_plans_dir(tmp_path, monkeypatch):
    """每个测试自动隔离：把 storage 模块的 _PLANS_DIR 指向 tmp_path。

    防御性（CLAUDE.md 量化风控·边界审查）：测试绝不污染真实 plans/ 目录，
    避免 CI 环境留下脏 plans JSON 干扰后续真实运行。
    """
    plans_dir = tmp_path / "plans"
    monkeypatch.setattr(storage, "_PLANS_DIR", str(plans_dir))
    yield


# ---------------------------------------------------------------------------
# 1. save_plans → load_plans 往返一致（含 Timestamp 序列化/反序列化）
# ---------------------------------------------------------------------------
class TestSaveLoadRoundtrip:
    """save_plans(date, [TradePlan]) → load_plans() 往返保真。

    核心断言：
        - 所有数值/字符串字段原样还原；
        - pd.Timestamp 字段（formed_at/valid_until/max_holding_until）经 ISO 字符串
          中转后还原为 pd.Timestamp（类型一致 + 值相等）；
        - metadata dict 完整保留。
    """

    def test_save_load_roundtrip_single(self):
        """单计划往返：save 后 load 返回的 dict 与原 TradePlan 字段一一对应。"""
        plan = _make_plan()
        storage.save_plans("2024-06-01", [plan])

        loaded = storage.load_plans()
        assert len(loaded) == 1
        d = loaded[0]

        # 数值与字符串字段原样还原
        assert d["plan_id"] == plan.plan_id
        assert d["symbol"] == plan.symbol
        assert d["pattern_type"] == plan.pattern_type
        assert d["breakout_price"] == pytest.approx(plan.breakout_price)
        assert d["H"] == pytest.approx(plan.H)
        assert d["shares"] == plan.shares

        # Timestamp 字段：ISO 字符串 → 还原为 pd.Timestamp（类型 + 值）
        assert isinstance(d["formed_at"], pd.Timestamp)
        assert d["formed_at"] == plan.formed_at
        assert isinstance(d["valid_until"], pd.Timestamp)
        assert d["valid_until"] == plan.valid_until
        assert isinstance(d["max_holding_until"], pd.Timestamp)
        assert d["max_holding_until"] == plan.max_holding_until

        # metadata dict 完整保留
        assert d["metadata"] == plan.metadata

    def test_save_load_roundtrip_multiple_with_metadata(self):
        """多计划 + 复杂 metadata 往返保真。

        TradePlan 是 frozen dataclass，metadata 覆盖用 dataclasses.replace
        （frozen 实例不可原地改，replace 返回新实例）。
        """
        base1 = _make_plan(plan_id="p-A", symbol="A.SZ")
        p1 = replace(base1, metadata={"depth": 0.1, "nested": {"k": [1, 2, 3]}})
        base2 = _make_plan(plan_id="p-B", symbol="B.SZ",
                           formed_at=pd.Timestamp("2024-06-02"))
        p2 = replace(base2, metadata={"depth": 0.2})
        storage.save_plans("2024-06-01", [p1, p2])

        loaded = storage.load_plans()
        assert len(loaded) == 2
        ids = {d["plan_id"] for d in loaded}
        assert ids == {"p-A", "p-B"}
        # 嵌套 metadata 完整保留
        nested = next(d for d in loaded if d["plan_id"] == "p-A")
        assert nested["metadata"]["nested"] == {"k": [1, 2, 3]}

    def test_save_plans_creates_dir_lazily(self):
        """plans/ 目录 lazy 创建（os.makedirs exist_ok=True）。

        删除目录后 save 仍能成功重建（_isolate_plans_dir 已指向 tmp_path/plans，
        初始不存在），断言落盘后目录 + 文件存在。
        """
        import os
        assert not os.path.exists(storage._PLANS_DIR)

        plan = _make_plan()
        storage.save_plans("2024-07-01", [plan])

        assert os.path.isdir(storage._PLANS_DIR)
        assert os.path.isfile(os.path.join(storage._PLANS_DIR, "2024-07-01.json"))


# ---------------------------------------------------------------------------
# 2. load_plans 按 status 过滤 + 跨日期合并
# ---------------------------------------------------------------------------
class TestLoadPlansFilterByStatus:
    """load_plans(status=...) 按 status 字段过滤，跨多日文件合并。"""

    def test_load_plans_filter_by_status_across_dates(self):
        """T 日 + T+1 日各存若干计划，按 status 过滤合并返回。

        构造：
          T 日：3 个计划（2 个 APPROVED + 1 个 PENDING_APPROVAL）
          T+1 日：2 个计划（1 个 APPROVED + 1 个 FILLED）
        查询 status="APPROVED" → 应返回 3 个（T 日 2 + T+1 日 1）。
        """
        # T 日
        p1 = _make_plan(plan_id="t1", symbol="T1.SZ")
        p2 = _make_plan(plan_id="t2", symbol="T2.SZ")
        p3 = _make_plan(plan_id="t3", symbol="T3.SZ")
        storage.save_plans("2024-06-01", [p1, p2, p3])
        # 手动设置 status（save_plans 默认全部 PENDING_APPROVAL）
        storage.update_plan("t1", status="APPROVED")
        storage.update_plan("t2", status="APPROVED")
        # t3 保持 PENDING_APPROVAL

        # T+1 日
        p4 = _make_plan(plan_id="t4", symbol="T4.SZ")
        p5 = _make_plan(plan_id="t5", symbol="T5.SZ")
        storage.save_plans("2024-06-02", [p4, p5])
        storage.update_plan("t4", status="APPROVED")
        storage.update_plan("t5", status="FILLED")

        # 按 status 过滤
        approved = storage.load_plans(status="APPROVED")
        assert len(approved) == 3
        approved_ids = {d["plan_id"] for d in approved}
        assert approved_ids == {"t1", "t2", "t4"}

        filled = storage.load_plans(status="FILLED")
        assert len(filled) == 1
        assert filled[0]["plan_id"] == "t5"

    def test_load_plans_default_status_none_returns_all(self):
        """status=None 返回所有计划（不过滤）。"""
        p1 = _make_plan(plan_id="a1", symbol="A1.SZ")
        storage.save_plans("2024-06-01", [p1])

        all_plans = storage.load_plans()
        assert len(all_plans) == 1

    def test_load_plans_empty_when_no_files(self):
        """无任何 plans 文件时返回空列表（不抛异常）。"""
        assert storage.load_plans() == []
        assert storage.load_plans(status="ARMED") == []


# ---------------------------------------------------------------------------
# 3. update_plan 状态迁移 + 字段更新
# ---------------------------------------------------------------------------
class TestUpdatePlanStatusTransition:
    """update_plan(plan_id, **fields) 状态迁移 + 任意字段更新。

    蔡森流水线状态机（brief）：
        PENDING_APPROVAL → APPROVED → ARMED → FILLED → CLOSED
    update_plan 是状态机驱动的唯一入口，下游执行器/审核器都通过它推进状态。
    """

    def test_update_plan_status_transition(self):
        """PENDING_APPROVAL → APPROVED → ARMED 状态迁移保真。"""
        plan = _make_plan(plan_id="s1", symbol="S1.SZ")
        storage.save_plans("2024-06-01", [plan])

        # 初始状态 = PENDING_APPROVAL（save_plans 默认）
        d = storage.get_plan("s1")
        assert d["status"] == "PENDING_APPROVAL"

        # PENDING_APPROVAL → APPROVED
        storage.update_plan("s1", status="APPROVED")
        d = storage.get_plan("s1")
        assert d["status"] == "APPROVED"

        # APPROVED → ARMED
        storage.update_plan("s1", status="ARMED")
        d = storage.get_plan("s1")
        assert d["status"] == "ARMED"

    def test_update_plan_arbitrary_field(self):
        """update_plan 支持任意字段更新（不止 status）。

        实盘场景：ARMED 后执行器回填实际成交价 fill_price、成交时间 filled_at、
        实际股数 actual_shares 等。这些字段在 save_plans 时不存在，update_plan
        应能增量添加。
        """
        plan = _make_plan(plan_id="f1", symbol="F1.SZ")
        storage.save_plans("2024-06-01", [plan])

        storage.update_plan("f1", status="FILLED",
                            fill_price=10.05, filled_at="2024-06-03", actual_shares=1000)

        d = storage.get_plan("f1")
        assert d["status"] == "FILLED"
        assert d["fill_price"] == pytest.approx(10.05)
        assert d["filled_at"] == "2024-06-03"
        assert d["actual_shares"] == 1000
        # 原字段保持不变
        assert d["symbol"] == "F1.SZ"
        assert d["shares"] == 1000

    def test_update_plan_nonexistent_raises(self):
        """更新不存在的 plan_id 抛 KeyError（防御性：状态机不进 NULL）。

        实盘风控（CLAUDE.md 量化风控·边界审查）：执行器回调更新一个不存在的计划
        是严重异常（状态机错位/消息乱序），应显式失败而非静默创建脏数据。
        """
        with pytest.raises(KeyError):
            storage.update_plan("nonexistent-id", status="APPROVED")

    def test_get_plan_nonexistent_returns_none(self):
        """get_plan 未命中返回 None（只读查询，不抛异常）。"""
        assert storage.get_plan("nonexistent") is None


# ---------------------------------------------------------------------------
# 4. add_to_cooldown / in_cooldown 形态失败冷却黑名单
# ---------------------------------------------------------------------------
class TestCooldownHitAndExpire:
    """形态失败冷却黑名单：add_to_cooldown(symbol, until_date) → in_cooldown 查询。

    蔡森方法学：假突破标的冷却——避免执行器在已失效形态上反复消耗。
    冷却语义：in_cooldown(symbol, date) 当 date <= until_date 时命中，
              date > until_date 时过期失效（until_date 当日仍冷却，次日释放）。
    """

    def test_cooldown_hit(self):
        """冷却命中：标的进入冷却期，查询日 ≤ until_date 返回 True。"""
        storage.add_to_cooldown("FAKE.SZ", "2024-06-10")

        assert storage.in_cooldown("FAKE.SZ", "2024-06-05") is True   # 冷却期内
        assert storage.in_cooldown("FAKE.SZ", "2024-06-10") is True   # until_date 当日仍冷却

    def test_cooldown_expire(self):
        """冷却过期：查询日 > until_date 返回 False（自动失效，无需手动清理）。"""
        storage.add_to_cooldown("FAKE.SZ", "2024-06-10")

        assert storage.in_cooldown("FAKE.SZ", "2024-06-11") is False  # 次日释放
        assert storage.in_cooldown("FAKE.SZ", "2024-07-01") is False  # 远期释放

    def test_cooldown_not_in_dict(self):
        """未加入冷却的标的：查询返回 False（空 cooldown.json 也安全）。"""
        assert storage.in_cooldown("NEVER.SZ", "2024-06-01") is False

    def test_cooldown_overwrite_later_date(self):
        """同标的多日 add_to_cooldown：后写覆盖前写（取最新 until_date）。

        实盘场景：标的首次假突破冷却 5 天，期间再次假突破，应延长冷却期。
        """
        storage.add_to_cooldown("FAKE.SZ", "2024-06-05")
        storage.add_to_cooldown("FAKE.SZ", "2024-06-20")  # 延长冷却

        assert storage.in_cooldown("FAKE.SZ", "2024-06-15") is True   # 新 until_date 内
        assert storage.in_cooldown("FAKE.SZ", "2024-06-25") is False  # 超出新 until_date

    def test_cooldown_multiple_symbols(self):
        """多标的独立冷却：互不干扰。"""
        storage.add_to_cooldown("A.SZ", "2024-06-10")
        storage.add_to_cooldown("B.SZ", "2024-06-20")

        assert storage.in_cooldown("A.SZ", "2024-06-08") is True
        assert storage.in_cooldown("B.SZ", "2024-06-08") is True
        # A 过期后 B 仍冷却
        assert storage.in_cooldown("A.SZ", "2024-06-15") is False
        assert storage.in_cooldown("B.SZ", "2024-06-15") is True

    def test_cooldown_persisted_to_file(self):
        """cooldown 数据落 cooldown.json（重启后仍可读，跨进程一致）。

        实盘部署：screen 进程与 execute 进程分离，cooldown 必须落盘共享。
        """
        storage.add_to_cooldown("PERSIST.SZ", "2024-06-10")

        import os
        cooldown_path = os.path.join(storage._PLANS_DIR, "cooldown.json")
        assert os.path.isfile(cooldown_path)
        with open(cooldown_path, encoding="utf-8") as f:
            raw = json.load(f)
        assert raw["PERSIST.SZ"] == "2024-06-10"


# ---------------------------------------------------------------------------
# 5. ARMED/FILLED 活跃计划落 active.json
# ---------------------------------------------------------------------------
class TestActivePlansPersistence:
    """活跃计划（ARMED/FILLED）落 active.json，供执行器/持仓监控高频读。

    设计意图：load_plans() 扫描所有 plans/<date>.json 适合低频审核，
    但实盘执行器需要 O(1) 定位"当前待执行/持仓中"的计划——active.json 是
    ARMED/FILLED 状态计划的扁平索引，update_plan 推进到这两个状态时自动同步。
    """

    def test_armed_plan_persisted_to_active(self):
        """计划进入 ARMED 状态 → active.json 包含该计划。"""
        plan = _make_plan(plan_id="arm1", symbol="ARM1.SZ")
        storage.save_plans("2024-06-01", [plan])
        storage.update_plan("arm1", status="ARMED")

        active = storage.load_active_plans()
        assert len(active) == 1
        assert active[0]["plan_id"] == "arm1"
        assert active[0]["status"] == "ARMED"

    def test_filled_plan_persisted_to_active(self):
        """计划进入 FILLED 状态 → active.json 包含该计划。"""
        plan = _make_plan(plan_id="fill1", symbol="FILL1.SZ")
        storage.save_plans("2024-06-01", [plan])
        storage.update_plan("fill1", status="FILLED")

        active = storage.load_active_plans()
        assert len(active) == 1
        assert active[0]["status"] == "FILLED"

    def test_closed_plan_removed_from_active(self):
        """计划进入 CLOSED 状态 → 从 active.json 移除（持仓已了结）。

        状态机：ARMED/FILLED → CLOSED，CLOSED 是终态，不应留在 active.json
        污染执行器读路径。
        """
        plan = _make_plan(plan_id="close1", symbol="CLOSE1.SZ")
        storage.save_plans("2024-06-01", [plan])
        storage.update_plan("close1", status="ARMED")
        assert len(storage.load_active_plans()) == 1

        storage.update_plan("close1", status="CLOSED")
        assert storage.load_active_plans() == []

    def test_pending_plan_not_in_active(self):
        """PENDING_APPROVAL/APPROVED 状态不在 active.json（尚未挂单）。"""
        plan = _make_plan(plan_id="pend1", symbol="PEND1.SZ")
        storage.save_plans("2024-06-01", [plan])
        # 默认 PENDING_APPROVAL
        assert storage.load_active_plans() == []

        storage.update_plan("pend1", status="APPROVED")
        assert storage.load_active_plans() == []

    def test_active_empty_when_no_file(self):
        """无 active.json 时 load_active_plans 返回空列表（不抛异常）。"""
        assert storage.load_active_plans() == []


# ---------------------------------------------------------------------------
# 6. save_plans date 严格 ISO 校验（B-2 路径遍历防御）
# ---------------------------------------------------------------------------
class TestSavePlansDateValidation:
    """save_plans(date) 对 date 做严格 YYYY-MM-DD 校验，防路径遍历/注入。

    安全背景（B-2）：date 直接拼进文件名 plans/<date>.json，若为自由字符串，
    攻击者可传 "../../../etc/cron.d/evil" 在任意路径写文件。校验必须：
        - 拒绝含路径分隔符 / ".." 的输入；
        - 拒绝非 ISO 格式（如 "2024/06/01"）；
        - 接受合法 "YYYY-MM-DD"。
    """

    def test_save_plans_rejects_path_traversal(self):
        """含 '../' 的 date 必须被拒（防路径遍历写任意路径）。"""
        with pytest.raises(ValueError, match="非法日期"):
            storage.save_plans("../../../etc/cron.d/evil", plans=[])

    def test_save_plans_rejects_backslash_traversal(self):
        """含反斜杠的 date 必须被拒（Windows 路径跳板）。"""
        with pytest.raises(ValueError):
            storage.save_plans("..\\..\\windows\\evil", plans=[])

    def test_save_plans_rejects_non_iso_separator(self):
        """非标准分隔符（/ 或 .）必须被拒（只接受 YYYY-MM-DD）。"""
        with pytest.raises(ValueError):
            storage.save_plans("2024/06/01", plans=[])

    def test_save_plans_rejects_invalid_month(self):
        """re 通过但语义非法（月份 13）必须被拒（二次 Timestamp 解析防御）。"""
        with pytest.raises(ValueError):
            storage.save_plans("2024-13-01", plans=[])

    def test_save_plans_accepts_legal_iso(self):
        """合法 YYYY-MM-DD 不抛且正常落盘。"""
        storage.save_plans("2024-06-01", plans=[])
        import os
        assert os.path.isfile(os.path.join(storage._PLANS_DIR, "2024-06-01.json"))
