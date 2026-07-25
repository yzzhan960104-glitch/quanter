# -*- coding: utf-8 -*-
"""约束裁剪测试（spec §7.1）：纯函数，快测，零 data_lake 依赖。"""


def test_param_keys_21_dims():
    """PARAM_KEYS 覆盖 21 维（11 识别 + 10 执行含 trailing 3）。"""
    from discovery.constraints import PARAM_KEYS
    assert len(PARAM_KEYS) == 21
    for k in ["window", "min_rr", "tp1_h_mult", "tp_h_mult",
              "cancel_thresh_mult", "trailing_grace", "trailing_step", "trailing_floor"]:
        assert k in PARAM_KEYS


def test_normalize_min_rr_fixed():
    """min_rr 是死参数（结构恒 2.0），normalize 后强制 2.0（spec §7.1 耦合2）。"""
    from discovery.constraints import normalize_params
    p = normalize_params({"min_rr": 1.5, "window": 80})
    assert p["min_rr"] == 2.0
    assert p["window"] == 80   # 其它参数不动


def test_normalize_trailing_grace_zero_freezes_step_floor():
    """grace=0 时 trailing 关闭（固定止损基线），step/floor 固定不搜（spec §7.1 耦合1）。

    物理意图：simulate_exit 的 trailing 仅在 grace>0 AND step>0 激活；grace=0 等价
    固定止损（=当前 EXEC_DEFAULTS 基线），step/floor 取任何值都不生效——搜它们是白跑。
    """
    from discovery.constraints import normalize_params
    p = normalize_params({"trailing_grace": 0, "trailing_step": 0.15, "trailing_floor": 0.5})
    assert p["trailing_grace"] == 0
    assert p["trailing_step"] == 0.0      # 固定为基线（不生效值）
    assert p["trailing_floor"] == 0.0


def test_normalize_trailing_grace_positive_keeps_step_floor():
    """grace>0 时 trailing 激活，step/floor 保留搜索值。"""
    from discovery.constraints import normalize_params
    p = normalize_params({"trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.5})
    assert p["trailing_grace"] == 5
    assert p["trailing_step"] == 0.1
    assert p["trailing_floor"] == 0.5


def test_is_feasible_tp1_le_tp_h():
    """tp1_h_mult ≤ tp_h_mult（防退化，spec §7.1 耦合3）。"""
    from discovery.constraints import is_feasible
    assert is_feasible({"tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is True
    assert is_feasible({"tp1_h_mult": 3.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is False


def test_is_feasible_cancel_ge_tp1():
    """cancel_thresh_mult ≥ tp1_h_mult（防过保守，spec §7.1 耦合4）；None=放飞不撤=合法。"""
    from discovery.constraints import is_feasible
    # cancel < tp1 → 非法（未到 tp1 就撤单 = 放弃突破）
    assert is_feasible({"tp1_h_mult": 1.5, "tp_h_mult": 2.5, "cancel_thresh_mult": 1.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is False
    # cancel ≥ tp1 → 合法
    assert is_feasible({"tp1_h_mult": 1.5, "tp_h_mult": 2.5, "cancel_thresh_mult": 2.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is True
    # cancel=None → 放飞不撤（颈线法默认语义）→ 合法
    assert is_feasible({"tp1_h_mult": 1.5, "tp_h_mult": 2.5, "cancel_thresh_mult": None,
                        "trailing_grace": 5, "trailing_step": 0.1}) is True


def test_is_feasible_uses_normalized_trailing():
    """is_feasible 在 grace=0 时不因 step/floor 报错（normalize 后这些值不参与判定）。"""
    from discovery.constraints import is_feasible
    # grace=0 + 任意 step/floor 都应合法（trailing 不生效）
    assert is_feasible({"tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
                        "trailing_grace": 0, "trailing_step": 0.99, "trailing_floor": 0.99}) is True


def test_filter_feasible_keeps_legal_drops_illegal():
    """filter_feasible 过滤整批：合法保留，非法丢弃，并先 normalize。"""
    from discovery.constraints import filter_feasible
    batch = [
        {"min_rr": 1.5, "tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
         "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0, "window": 80},  # 合法
        {"min_rr": 2.0, "tp1_h_mult": 3.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
         "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0, "window": 60},  # 非法（tp1>tp_h）
        {"min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 0.5,
         "trailing_grace": 0, "trailing_step": 0.15, "trailing_floor": 0.5, "window": 40},  # 非法（cancel<tp1）
    ]
    kept = filter_feasible(batch)
    assert len(kept) == 1
    assert kept[0]["min_rr"] == 2.0          # normalize 已固定死参数
    assert kept[0]["window"] == 80
    # 第3条 grace=0 本应被 normalize 救回 trailing，但 cancel<tp1 仍非法 → 被滤


def test_coupling5_suppression_decay_tau_independent():
    """耦合5（design 决策5）：suppression/decay_tau 独立可调，normalize 不强行捆绑。

    代码实证（method_v0.py:163-173）：decay_tau=None 等权时 suppression 仍生效；spec §7.1
    '捆绑调'语义已退化为'都可调'——凭空裁剪误杀合法组合，故 Plan 3 仅厘清不裁。
    """
    from discovery.constraints import normalize_params
    # decay_tau=None（等权）+ suppression 非 0 → 都保留
    p = normalize_params({"min_suppression": 0.5, "decay_tau": None})
    assert p["min_suppression"] == 0.5
    assert p["decay_tau"] is None
    # decay_tau=30 + suppression=0 → 也都保留（不强制捆绑）
    p2 = normalize_params({"min_suppression": 0.0, "decay_tau": 30})
    assert p2["decay_tau"] == 30
    assert p2["min_suppression"] == 0.0
