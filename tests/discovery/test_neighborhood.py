# -*- coding: utf-8 -*-
"""邻域稳定性测试：perturb_params 结构 + neighborhood_stability 字段（合成 universe）。

物理意图（spec §12⑥ / 分层裁判 L3）：冠军 21 维数值参数 ±perturb 扰动，看 outer calmar
稳定性——高原（邻域 calmar 不塌）= 稳健放行；孤峰（塌或方差爆炸）= 过拟合尖峰否决。
本测试用合成 universe（synth_sym_df 单票 250 根）跑通逻辑，不依赖真实 data_lake（快，
非 slow）。手动验收跑 cli verify（~15min）才用真 universe。
"""
import random


def test_perturb_params_keeps_none():
    """None 参数（cancel_thresh_mult/decay_tau）不扰动，保留原值。

    物理意图：None 在颈线法语义是"用内核默认/不启用该机制"（如 cancel_thresh_mult=None
    =不限撤单），扰动 None 会破坏语义——必须保留 None。
    """
    from discovery.neighborhood import perturb_params
    p = {"window": 80, "decay_tau": None, "cancel_thresh_mult": None}
    rng = random.Random(0)
    nb = perturb_params(p, 0.15, rng, n_dims=1)
    assert nb["decay_tau"] is None
    assert nb["cancel_thresh_mult"] is None
    assert isinstance(nb["window"], (int, float))


def test_perturb_params_changes_numeric():
    """数值参数扰动后至少一个被改（×[0.5,1.5] 大概率改变值）。

    物理意图：扰动必须真扰动到值，否则邻域采样退化成"原地重复"——无法检验稳定性。
    perturb=0.5（±50%）下 2 维参数几乎必然至少一个变。
    """
    from discovery.neighborhood import perturb_params
    p = {"window": 80, "stop_atr_mult": 1.0}
    rng = random.Random(0)
    nb = perturb_params(p, 0.5, rng, n_dims=2)
    # 至少一个数值参数被改（×[0.5,1.5]）
    changed = any(nb[k] != p[k] for k in p)
    assert changed


def test_perturb_params_keeps_int_type():
    """int 参数（window/min_touches 等）扰动后 round 保 int——否则 scan_symbol 的
    range(window) 收到 float 会 TypeError（颈线法内核硬依赖 int window）。"""
    from discovery.neighborhood import perturb_params
    p = {"window": 80, "min_touches": 3, "stop_atr_mult": 1.5}
    rng = random.Random(1)
    # n_dims=3 扰到所有数值键，验证 int 键 round 后仍是 int
    nb = perturb_params(p, 0.3, rng, n_dims=3)
    assert isinstance(nb["window"], int), f"window 应保 int，实际 {type(nb['window'])}"
    assert isinstance(nb["min_touches"], int), f"min_touches 应保 int，实际 {type(nb['min_touches'])}"
    assert isinstance(nb["stop_atr_mult"], float)  # float 键保持 float


def test_neighborhood_stability_fields(champion_params, synth_sym_df):
    """合成 universe 跑 neighborhood_stability，验证返回字段齐全（ann 值不验证，合成数据）。

    物理意图：验证 neighborhood_stability 返回结构齐全（base/邻域 list/mean/std/is_plateau/
    base_outer），下游 cli verify 与人审据此判高原/孤峰。合成 universe 的指标绝对值无意义，
    只验字段与 list 长度。
    """
    from discovery.neighborhood import neighborhood_stability
    from discovery.split import holdout_split
    universe = {"300001.SZ": synth_sym_df}
    stab = neighborhood_stability(champion_params, universe, holdout_split(),
                                  perturb=0.2, n_samples=3)
    assert set(stab.keys()) >= {"base_calmar", "neighbor_calmars", "neighbor_mean",
                                "std", "is_plateau", "base_outer"}
    assert len(stab["neighbor_calmars"]) == 3
    assert isinstance(stab["is_plateau"], bool)
    # base_outer 应是 evaluate 的 outer 段 dict（含 calmar/ann/sharpe/max_dd/n）
    assert "calmar" in stab["base_outer"]
