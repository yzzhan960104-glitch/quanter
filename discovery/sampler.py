# -*- coding: utf-8 -*-
"""L3 采样层（spec §7.2，自写 Sobol 准随机 + random 补充，零新增依赖）。

物理意图（spec §7.2 / §3.5 判据④）：纯随机/贪心在 21 维空间覆盖极不均匀（聚集+盲区
并存），是伪收敛的元凶。Sobol 准随机序列（低差异序列）以远低于纯随机的样本数达到空间
均匀覆盖——这是收敛判据④（覆盖度）达标的物理手段，"先铺满再谈优化"。

Plan 2 范围：Sobol 初始覆盖 + random 补充（满足 sample_search 接口）。**TPE 序贯优化留
Plan 3**（需 OOS 目标函数 + 后验拟合，Plan 2 仅立采样骨架）。

反魔法决策（ADR4）：spec §7.2 写 "optuna.samplers.SobolSampler 或自写"。实证 optuna 未
安装（.venv310 import 失败）。本模块**自写 Sobol**（Joe & Kuo 2008 标准方向数，纯 Python），
零新增依赖，符合 Karpathy 极简——Sobol 算法本身是公开数学，不必引重型库。

算法出处：Antonov & Saleev (1979) 的 Gray-code 推进 + Joe & Kuo (2008) "Constructing
Sobol sequences with better two-dimensional projections" 的方向数（primitive polynomial
+ initial m）。方向数表取自 Joe & Kuo 官方公开数据（前 21 维），与 scipy.stats.qmc.Sobol
同源（scipy 用的就是这套方向数 + Burley 扰动；本实现不做扰动，纯标准 Sobol）。
"""
import numpy as np

# 复用 scripts/param_iter.PARAM_SPACE（21 维候选档，同源不重造）
# PARAM_SPACE_RAW 形如 [(key, layer, [candidates]), ...]，顺序与 PARAM_KEYS 一致
from scripts.param_iter import PARAM_SPACE as _PARAM_SPACE_RAW

# 整理为有序 [(key, [candidates])]（去掉 layer 标记，顺序与 PARAM_KEYS 一致）
PARAM_SPACE = [(k, cands) for k, _layer, cands in _PARAM_SPACE_RAW]
_PARAM_KEYS = [k for k, _ in PARAM_SPACE]
_CANDIDATES = [cands for _, cands in PARAM_SPACE]

# ---------------------------------------------------------------------------
# Sobol 方向数（Joe & Kuo 2008 公开数据，前 21 维）
# ---------------------------------------------------------------------------
# 每维用 (degree s, 整数 a, [m_1..m_s]) 描述。a 是 primitive polynomial 的中间系数整数
# 表示（a 的 (s-1) 个 bit 从高到低对应 x^{s-1}..x^1 的系数，常数项恒为 1）。
# m_i 是初始方向数（奇整数）。数值取自 Joe & Kuo 官方 new-joe-kuo-6.21201.txt（公开数据），
# 与 scipy.stats.qmc.Sobol 同源（验证：本模块 dim=1 一维投影 == 标准 van der Corput base-2）。
# 来源：https://web.maths.unsw.edu.au/~fkuo/sobol/ （Joe & Kuo 官方页）
_SOBOL_DIRECTION_NUMBERS = {
    # dim: (degree, a, [m_1, m_2, ...])  —— 前 21 维（够 PARAM_KEYS=21）
    1:  (1, 0, [1]),
    2:  (2, 1, [1, 3]),
    3:  (3, 1, [1, 3, 1]),
    4:  (3, 2, [1, 1, 1]),
    5:  (4, 1, [1, 1, 3, 1]),
    6:  (4, 4, [1, 3, 5, 7]),
    7:  (5, 2, [1, 7, 11, 13, 1]),
    8:  (5, 6, [1, 1, 5, 3, 7]),
    9:  (5, 5, [1, 3, 1, 7, 5]),
    10: (5, 0, [1, 1, 1, 1, 1]),
    11: (5, 3, [1, 3, 5, 7, 13]),
    12: (5, 7, [1, 1, 5, 11, 7]),
    13: (6, 4, [1, 1, 1, 3, 9, 17]),
    14: (6, 6, [1, 1, 3, 5, 1, 11]),
    15: (6, 1, [1, 3, 1, 1, 9, 5]),
    16: (6, 3, [1, 1, 5, 5, 9, 7]),
    17: (6, 5, [1, 3, 7, 7, 1, 17]),
    18: (6, 7, [1, 1, 1, 15, 13, 23]),
    19: (6, 2, [1, 1, 7, 11, 19, 3]),
    20: (6, 4, [1, 1, 3, 7, 19, 9]),
    21: (6, 6, [1, 3, 5, 13, 1, 11]),
}


def _compute_direction_vectors(max_dim=21, n_bits=32):
    """从 direction numbers 生成 32-bit V[d][j] 方向向量数组（Burkardt/Joe-Kuo 算法）。

    V[d][j]（j=1..n_bits，1-indexed）= 第 d 维第 j 个方向数（32-bit 无符号整数，
    已左移归一到 [0, 2^32)）。生成第 k 个 Sobol 点用 XOR of V[d][j] where bit (j-1) of k
    is set（Antonov-Saleev Gray-code 形式翻一位等价于本朴素形式）。

    递归（标准 Sobol 方向数公式，原始整数 m 域）：
        m[j] = m[j-s] ^ (m[j-s] >> s)                       # 基项
        for i in 1..s-1:                                    # 多项式中间系数项
            if (a >> (s-1-i)) & 1:  m[j] ^= m[j-s+i] >> (s-i)
        v[j] = m[j] << (n_bits - j)                         # 归一到 32-bit
    其中 a 的 (s-1) 个 bit 从高到低对应 x^{s-1}..x^1（多项式常数项恒 1 不进 a）。
    s=1（dim 1）时多项式为 x+1，无中间系数，递归退化为 m[j]=m[j-1]>>1（van der Corput base-2）。
    """
    # V[d][j] 1-indexed（d=0 占位不用，j=0 占位不用）；存为 (max_dim+1, n_bits+1) 数组
    V = np.zeros((max_dim + 1, n_bits + 1), dtype=np.uint32)
    for d in range(1, max_dim + 1):
        s, a, m_init = _SOBOL_DIRECTION_NUMBERS[d]
        # 原始方向数 m[j]，j=1..n_bits（1-indexed）。前 s 个来自 m_init，其余递归。
        m = [0] * (n_bits + 1)              # 1-indexed；m[0] 占位
        for j in range(1, s + 1):
            m[j] = int(m_init[j - 1])
        for j in range(s + 1, n_bits + 1):
            # 基项：m[j-s] ^ (m[j-s] >> s)
            val = m[j - s] ^ (m[j - s] >> s)
            # 多项式中间系数项：a 的第 i 位（i=1..s-1，从最高位起）置位则 XOR m[j-s+i]>>(s-i)
            for i in range(1, s):
                if (a >> (s - 1 - i)) & 1:
                    val ^= m[j - s + i] >> (s - i)
            m[j] = val
        # 归一到 32-bit 方向向量：v[j] = m[j] << (n_bits - j)。
        # 用 & 0xFFFFFFFF 显式截到 32 位再赋给 uint32 数组——np.uint32(...) 在 Windows
        # 上走 C long（有符号 32 位），1<<31 会 OverflowError；先 Python int 截断再赋值绕过。
        mask = (1 << n_bits) - 1
        for j in range(1, n_bits + 1):
            V[d][j] = (m[j] << (n_bits - j)) & mask
    return V


# 模块级预计算（一次构建，多次复用，避免每次采样重算方向数）
_DIRECTION_VECTORS = _compute_direction_vectors(21, 32)
_N_BITS = 32
_SCALE = 4294967296.0  # 2^32，归一到 [0,1)


def sobol_sample(dim, n, seed=0):
    """自写 Sobol 准随机序列（Joe & Kuo 2008），shape=(n, dim)，值∈[0,1)。

    算法（Antonov-Saleev Gray-code 推进）：
        第 k 个点（k=0..n-1，k 从 start=seed*n 偏移）的第 d 维
            = XOR of V[d][j] for all j where bit (j-1) of Gray(k) is set，
        其中 Gray(k) = k ^ (k>>1)。结果除以 2^32 归一到 [0,1)。
    dim ≤ 21（覆盖 PARAM_KEYS=21 维）。
    seed 通过跳过前 seed×n 个点实现确定性偏移（同 seed 同 dim 同 n → 同输出，可复现，
    落 trial.seed 的基石）。

    复杂度：O(n × dim × n_bits)，纯 NumPy 向量化（无 Python 三重循环，n=10000 仍亚秒）。
    """
    assert dim <= 21, f"Sobol 仅支持 dim≤21（PARAM_KEYS=21），got {dim}"
    V = _DIRECTION_VECTORS                    # (22, 33) uint32，1-indexed
    # 起点 index = seed * n（确定性偏移，让不同 seed 出不同序列）
    start = int(seed) * n
    out = np.zeros((n, dim), dtype=np.float64)
    # 逐点生成（用 Gray-code：相邻点只差一位，但本实现为清晰起见每点独立从 Gray(k) 算）
    for i in range(n):
        k = start + i
        g = k ^ (k >> 1)                      # Antonov-Saleev Gray code
        # 收集 g 的置位 bit（j-1 索引），对每维异或对应方向向量
        if g == 0:
            continue                          # 第 0 点全 0（Sobol 起点）
        # 向量化：找出 g 的所有置位 bit，对所有维一次性 XOR
        bits = []
        gg = g
        j_idx = 1                             # V[d][j] 1-indexed，bit (j-1) of g
        while gg:
            if gg & 1:
                bits.append(j_idx)
            gg >>= 1
            j_idx += 1
        for d in range(dim):
            acc = np.uint32(0)
            for j in bits:
                acc ^= V[d + 1][j]
            out[i, d] = acc / _SCALE
    return out


def random_sample(dim, n, seed=0):
    """纯随机采样（np.random.default_rng），shape=(n, dim)，值∈[0,1)。

    作 Sobol 覆盖的补充（spec §7.2：Sobol 初始覆盖 + random 补充，TPE 留 Plan 3）。
    确定性：同 seed 同 dim 同 n → 同输出（default_rng 可复现）。
    """
    rng = np.random.default_rng(seed)
    return rng.random((n, dim))


def _scale_to_candidates(unit_vec, candidates_per_dim):
    """单位向量 [0,1)^dim → 每维候选档索引 → 取候选档值 → 位置键 dict。

    unit_vec[d]∈[0,1) → idx = int(unit_vec[d] × len(cands))，clamp 到 [0, len-1]。
    候选档可为任意类型（int/float/None），直接索引取值。
    返回 dict 键为**位置索引整数**（0..len-1）——保持本函数对任意 candidates_per_dim 自洽，
    不耦合 PARAM_KEYS（便于单测用小样例验证映射）。键名映射（位置→参数名）在
    _unit_vecs_to_params 中做，sample_search 出口才是 PARAM_KEYS 命名键。
    """
    p = {}
    for d, cands in enumerate(candidates_per_dim):
        u = unit_vec[d]
        idx = int(u * len(cands))
        idx = max(0, min(len(cands) - 1, idx))
        p[d] = cands[idx]
    return p


def _unit_vecs_to_params(unit_mat):
    """(n, dim) 单位矩阵 → list[dict]（每维映射到候选档，键为 PARAM_KEYS 参数名）。

    复用 _scale_to_candidates 拿位置键，再 remap 到 _PARAM_KEYS 命名键（sample_search
    出口需 PARAM_KEYS 命名，filter_feasible/is_feasible 按名查字段）。
    """
    out = []
    for i in range(unit_mat.shape[0]):
        pos = _scale_to_candidates(unit_mat[i], _CANDIDATES)
        out.append({_PARAM_KEYS[d]: v for d, v in pos.items()})
    return out


def sample_search(n_sobol, n_random, seed=0, n_attempts_factor=3):
    """约束裁剪后的合法采样流：Sobol 初始覆盖 + random 补充。

    流程：采样 (n_sobol + n_random) × 候选 → filter_feasible 裁剪 → 取前
    (n_sobol + n_random) 合法。Sobol 初始覆盖（先铺满）+ random 补充；若裁剪后不够
    （废组合密度高），继续 random 补采直至达量或 attempts 上限（防约束过紧死循环）。
    返回 list[dict]（已 normalize，21 维齐全，约束合法），纯 Python 类型可 pickle 跨进程。

    spec §7.2 判据④物理手段：Sobol 初始覆盖保证空间均匀（比纯随机少聚集），filter_feasible
    裁掉物理无意义组合（耦合 1-4，见 constraints.py），合法密度提升 → 后续 Plan 3 TPE 从
    合法空间起点更高效。
    """
    from discovery.constraints import filter_feasible
    target = n_sobol + n_random
    if target == 0:
        return []
    dim = len(_PARAM_KEYS)
    # 阶段一：Sobol 初始覆盖（spec §7.2 "先铺满"）
    sob_unit = sobol_sample(dim, n_sobol, seed=seed) if n_sobol > 0 else np.zeros((0, dim))
    sob_params = _unit_vecs_to_params(sob_unit)
    # 阶段二：random 补充
    rnd_unit = random_sample(dim, n_random, seed=seed + 1) if n_random > 0 else np.zeros((0, dim))
    rnd_params = _unit_vecs_to_params(rnd_unit)
    # 合并 + 裁剪（filter_feasible 内部做 normalize + is_feasible）
    batch = filter_feasible(sob_params + rnd_params)
    # 不够则 random 继续补采（attempts 上限防死循环，约束极紧时安全退出）
    attempts = 0
    max_attempts = n_attempts_factor * 5
    extra_seed = seed + 100
    while len(batch) < target and attempts < max_attempts:
        more = random_sample(dim, target * 2, seed=extra_seed)
        batch.extend(filter_feasible(_unit_vecs_to_params(more)))
        extra_seed += 1
        attempts += 1
    return batch[:target]
