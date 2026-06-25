# -*- coding: utf-8 -*-
"""
基于隐马尔可夫模型（HMM）的 FICC 宏观状态识别模块

核心功能：
1. 混频数据对齐（日频市场数据 + 月频宏观数据），严格防未来函数
2. HMM 模型训练与预测，输出状态概率矩阵
3. 不确定性度量（香农熵）

设计原则：
- 显式数据对齐逻辑，不依赖黑箱
- 异常值显式处理（NaN 检查、数据清洗）
- 置信度阈值保护极端场景

作者：量化交易团队
日期：2026-06-25
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM


class MacroRegimeHMM:
    """
    基于 HMM 的宏观状态识别模型

    功能：
    1. 对齐日频与月频数据（防未来函数）
    2. 训练 HMM 模型识别隐藏宏观状态
    3. 预测每日状态概率矩阵
    4. 计算不确定性度量（香农熵）

    示例：
        >>> hmm = MacroRegimeHMM(n_components=3, covariance_type="full")
        >>> daily_df, macro_df = hmm.generate_mock_data()
        >>> aligned_df = hmm.align_macro_data(daily_df, macro_df)
        >>> hmm.fit(aligned_df)
        >>> prob_matrix, entropy = hmm.predict(aligned_df)
    """

    def __init__(
        self,
        n_components: int = 3,
        covariance_type: str = "full",
        n_iter: int = 100,
        random_state: Optional[int] = None,
        confidence_threshold: float = 0.5,
    ):
        """
        初始化 HMM 模型

        参数：
            n_components: 隐藏状态数量（建议 3：扩张/衰退/平稳）
            covariance_type: 协方差类型（"spherical"/"diag"/"tied"/"full"）
            n_iter: EM 算法最大迭代次数
            random_state: 随机种子
            confidence_threshold: 置信度阈值，低于此值标记为不确定性高

        注意：
            - "full" 协方差类型更灵活但参数更多，容易过拟合
            - "diag" 协方差类型更稳定，适合数据量较少的场景
        """
        self.n_components = n_components
        self.covariance_type = covariance_type
        self.n_iter = n_iter
        self.random_state = random_state
        self.confidence_threshold = confidence_threshold

        # HMM 模型实例
        self.model: Optional[GaussianHMM] = None

        # 训练元数据
        self.feature_names_: Optional[List[str]] = None
        self.is_fitted_: bool = False

        # 初始化模型
        self._init_model()

    def _init_model(self) -> None:
        """初始化 HMM 模型实例"""
        self.model = GaussianHMM(
            n_components=self.n_components,
            covariance_type=self.covariance_type,
            n_iter=self.n_iter,
            random_state=self.random_state,
        )

    def align_macro_data(
        self,
        daily_df: pd.DataFrame,
        macro_df: pd.DataFrame,
        release_lag: int = 0,
        max_fill_days: int = 90,
    ) -> pd.DataFrame:
        """
        对齐日频与月频宏观数据（严格防未来函数）

        核心逻辑：
        1. 将月频宏观数据按照实际发布日期对齐到日频时间轴
        2. 无宏观数据更新的交易日使用前向填充（ffill）
        3. 记录数据"新鲜度"（距离最后一次宏观数据发布的天数）

        防范未来函数：
        - 宏观数据在发布当天才可用，不使用"数据发生时间"
        - 使用 release_lag 参数可手动调整发布滞后天数

        参数：
            daily_df: 日频数据 DataFrame（必须有 DatetimeIndex）
            macro_df: 月频宏观数据 DataFrame（必须有 DatetimeIndex）
            release_lag: 发布滞后天数（模拟数据实际公布延迟）
            max_fill_days: 最大前向填充天数，超过则标记为 NaN

        返回：
            对齐后的 DataFrame（包含日频数据 + ffill 后的宏观数据 + 新鲜度指标）

        异常：
            ValueError: 输入数据包含 NaN 未处理或索引格式错误
        """
        # ============ 输入验证 ============
        if not isinstance(daily_df.index, pd.DatetimeIndex):
            raise ValueError("daily_df 的索引必须是 DatetimeIndex")
        if not isinstance(macro_df.index, pd.DatetimeIndex):
            raise ValueError("macro_df 的索引必须是 DatetimeIndex")

        # 检查 NaN 并显式处理
        if daily_df.isna().any().any():
            nan_cols = daily_df.columns[daily_df.isna().any()].tolist()
            raise ValueError(
                f"daily_df 包含 NaN，请在对齐前清理数据。NaN 列：{nan_cols}"
            )
        if macro_df.isna().any().any():
            nan_cols = macro_df.columns[macro_df.isna().any()].tolist()
            raise ValueError(
                f"macro_df 包含 NaN，请在对齐前清理数据。NaN 列：{nan_cols}"
            )

        # ============ 数据复制（避免修改原始数据） ============
        aligned_df = daily_df.copy()

        # ============ 调整宏观数据发布日期（模拟滞后） ============
        macro_adjusted = macro_df.copy()
        if release_lag > 0:
            # 将宏观发布日期向后推迟 release_lag 天
            macro_adjusted.index = macro_adjusted.index + pd.Timedelta(days=release_lag)

        # ============ 对齐宏观数据到日频时间轴 ============
        for col in macro_df.columns:
            # 创建一个以日频索引为模板的空序列
            macro_series_aligned = pd.Series(index=daily_df.index, dtype=float)

            # 将月频数据映射到日频（发布当天生效）
            for date, value in macro_adjusted[col].items():
                # 确保发布日期在日频范围内
                if date in daily_df.index:
                    macro_series_aligned.loc[date] = value

            # 前向填充（沿用到下一个宏观数据发布前）
            macro_series_aligned = macro_series_aligned.ffill()

            # 记录数据新鲜度（距离最后一次宏观数据发布的天数）
            freshness = (
                pd.Series(index=daily_df.index, dtype=int)
                .astype(float)
                .fillna(0)
                .astype(int)
            )

            last_release_date = None
            for i, date in enumerate(macro_series_aligned.index):
                if date in macro_adjusted.index:
                    last_release_date = date
                    freshness.iloc[i] = 0
                elif last_release_date is not None:
                    freshness.iloc[i] = (date - last_release_date).days
                else:
                    # 第一个宏观数据发布之前，标记为最大值
                    freshness.iloc[i] = max_fill_days + 1

            # 超过最大填充天数的日期标记为 NaN
            macro_series_aligned[freshness > max_fill_days] = np.nan

            # 添加到对齐后的 DataFrame
            aligned_df[col] = macro_series_aligned

            # 添加新鲜度指标
            aligned_df[f"{col}_freshness"] = freshness

        # ============ 再次检查是否有遗漏的 NaN ============
        if aligned_df.isna().any().any():
            nan_cols = aligned_df.columns[aligned_df.isna().any()].tolist()
            nan_counts = aligned_df.isna().sum()[aligned_df.isna().sum() > 0]
            print(
                f"警告：对齐后仍有 NaN（可能是宏观数据初始期缺失），列：{nan_cols}，"
                f"各列缺失数：\n{nan_counts}"
            )

        return aligned_df

    def fit(
        self,
        data: pd.DataFrame,
        feature_columns: Optional[List[str]] = None,
        drop_na: bool = True,
    ) -> None:
        """
        训练 HMM 模型

        参数：
            data: 训练数据 DataFrame（必须是数值型）
            feature_columns: 使用的特征列（默认为所有数值列）
            drop_na: 是否删除包含 NaN 的行

        返回：
            None（模型状态更新为 is_fitted_=True）

        异常：
            ValueError: 数据量不足以训练模型或特征为空
        """
        # ============ 特征选择 ============
        if feature_columns is None:
            feature_columns = data.select_dtypes(include=[np.number]).columns.tolist()

        if not feature_columns:
            raise ValueError("未找到有效的数值型特征列")

        self.feature_names_ = feature_columns

        # ============ 数据预处理 ============
        X = data[feature_columns].copy()

        if drop_na:
            X = X.dropna()

        if len(X) == 0:
            raise ValueError("删除 NaN 后数据为空，无法训练模型")

        if len(X) < self.n_components * 2:
            raise ValueError(
                f"数据量不足（{len(X)} 行），建议至少 {self.n_components * 2} 行"
            )

        # ============ 标准化（防止数值范围差异过大导致 HMM 不稳定） ============
        X_mean = X.mean()
        X_std = X.std()
        X_std[X_std == 0] = 1.0  # 防止除以零
        X_normalized = (X - X_mean) / X_std

        # ============ 训练 HMM 模型 ============
        # hmmlearn 接受 shape=(n_samples, n_features) 的二维数组
        X_array = X_normalized.values

        try:
            self.model.fit(X_array)
        except ValueError as e:
            raise ValueError(f"HMM 训练失败：{str(e)}")

        # ============ 更新状态 ============
        self.is_fitted_ = True
        self.X_mean_ = X_mean
        self.X_std_ = X_std

        print(f"HMM 模型训练成功，状态数：{self.n_components}")

    def predict(
        self,
        data: pd.DataFrame,
        drop_na: bool = False,
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """
        预测状态概率矩阵与不确定性度量

        参数：
            data: 预测数据 DataFrame
            drop_na: 是否删除包含 NaN 的行（False 则跳过 NaN 行）

        返回：
            prob_matrix: 状态概率矩阵（n_rows × n_components），每行为各状态概率
            entropy: 香农熵序列，衡量预测不确定性（值越大越不确定）

        异常：
            RuntimeError: 模型未训练
        """
        # ============ 状态检查 ============
        if not self.is_fitted_:
            raise RuntimeError("模型未训练，请先调用 fit() 方法")

        # ============ 数据预处理 ============
        X = data[self.feature_names_].copy()

        if drop_na:
            X = X.dropna()
            original_index = X.index
        else:
            # 跳过包含 NaN 的行
            valid_mask = ~X.isna().any(axis=1)
            X = X[valid_mask]
            original_index = X.index

        if len(X) == 0:
            raise ValueError("预测数据为空")

        # ============ 标准化（使用训练集的均值和标准差） ============
        X_normalized = (X - self.X_mean_) / self.X_std_

        # ============ 预测状态概率矩阵 ============
        X_array = X_normalized.values
        prob_matrix = self.model.predict_proba(X_array)

        # ============ 计算香农熵（不确定性度量） ============
        # 熵公式：H = -∑ p_i * log(p_i)
        # 当概率分布均匀（如 [0.33, 0.33, 0.34]）时，熵最大（最不确定）
        # 当概率分布极端（如 [1.0, 0.0, 0.0]）时，熵为 0（最确定）
        epsilon = 1e-10  # 防止 log(0)
        entropy = -np.sum(prob_matrix * np.log(prob_matrix + epsilon), axis=1)

        # ============ 封装结果 ============
        prob_df = pd.DataFrame(
            prob_matrix,
            index=original_index,
            columns=[f"state_{i}_prob" for i in range(self.n_components)],
        )

        entropy_series = pd.Series(entropy, index=original_index, name="uncertainty_entropy")

        return prob_df, entropy_series

    def predict_with_confidence(
        self,
        data: pd.DataFrame,
        drop_na: bool = False,
    ) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
        """
        预测状态概率矩阵、不确定性度量与置信度指标

        参数：
            data: 预测数据 DataFrame
            drop_na: 是否删除包含 NaN 的行

        返回：
            prob_matrix: 状态概率矩阵
            entropy: 香农熵序列
            confidence: 置信度序列（1 = 最大概率 > 阈值，0 = 否）
        """
        prob_matrix, entropy = self.predict(data, drop_na)

        # 计算置信度：最大概率是否超过阈值
        max_prob = prob_matrix.max(axis=1)
        confidence = (max_prob >= self.confidence_threshold).astype(int)

        return prob_matrix, entropy, confidence

    def get_state_labels(self) -> Dict[int, str]:
        """
        获取状态标签（需手动解释）

        根据 HMM 训练结果，人工解释各状态的宏观含义：
        - 状态 0: 经济扩张期
        - 状态 1: 经济衰退期
        - 状态 2: 经济平稳期

        注意：HMM 标签是随机初始化的，需要根据训练数据解释

        返回：
            状态标签字典
        """
        # 默认标签（需根据实际训练结果调整）
        labels = {
            0: "经济扩张期",
            1: "经济衰退期",
            2: "经济平稳期",
        }

        # 确保标签数量匹配状态数
        if len(labels) != self.n_components:
            labels = {i: f"状态_{i}" for i in range(self.n_components)}

        return labels


# ============ 测试用例 ============

def generate_mock_data() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    生成模拟数据（日频 + 月频）

    返回：
        daily_df: 日频数据（包含收益率、汇率等）
        macro_df: 月频数据（包含 PMI、CPI 等）
    """
    np.random.seed(42)

    # ============ 生成日频数据（2 年） ============
    n_days = 730  # 约 2 年
    dates_daily = pd.date_range(start="2023-01-01", periods=n_days, freq="B")  # 工作日

    # 生成国债收益率（随机游走）
    yield_rate = 2.5 + np.cumsum(np.random.normal(0, 0.05, n_days))
    yield_rate = np.clip(yield_rate, 0, 10)  # 限制在 [0, 10]% 范围

    # 生成汇率（人民币对美元）
    exchange_rate = 7.0 + np.cumsum(np.random.normal(0, 0.01, n_days))
    exchange_rate = np.clip(exchange_rate, 6.5, 8.0)  # 限制在 [6.5, 8.0]

    daily_df = pd.DataFrame(
        {
            "yield_rate": yield_rate,
            "exchange_rate": exchange_rate,
        },
        index=dates_daily,
    )

    # ============ 生成月频宏观数据 ============
    n_months = 24  # 2 年
    dates_macro = pd.date_range(start="2023-01-01", periods=n_months, freq="MS")  # 月初

    # 生成 PMI（采购经理指数，围绕 50 波动）
    pmi = 50 + np.random.normal(0, 5, n_months)
    pmi = np.clip(pmi, 30, 70)  # 限制在 [30, 70] 范围

    # 生成 CPI（消费者物价指数，围绕 2% 波动）
    cpi = 2.0 + np.random.normal(0, 0.5, n_months)
    cpi = np.clip(cpi, -1.0, 5.0)  # 限制在 [-1.0, 5.0]% 范围

    macro_df = pd.DataFrame(
        {
            "pmi": pmi,
            "cpi": cpi,
        },
        index=dates_macro,
    )

    return daily_df, macro_df


def test_hmm_macro_module() -> None:
    """
    完整测试用例

    测试流程：
    1. 生成模拟数据（日频 + 月频）
    2. 对齐混频数据
    3. 训练 HMM 模型
    4. 预测状态概率矩阵
    5. 计算不确定性度量
    """
    print("=" * 60)
    print("HMM 宏观状态识别模块测试")
    print("=" * 60)

    # ============ 步骤 1：生成模拟数据 ============
    print("\n[1/5] 生成模拟数据...")
    daily_df, macro_df = generate_mock_data()
    print(f"  - 日频数据：{len(daily_df)} 行，{len(daily_df.columns)} 列")
    print(f"  - 月频数据：{len(macro_df)} 行，{len(macro_df.columns)} 列")

    # ============ 步骤 2：对齐混频数据 ============
    print("\n[2/5] 对齐混频数据...")
    hmm = MacroRegimeHMM(
        n_components=3,
        covariance_type="diag",  # 使用对角协方差，更稳定
        random_state=42,
        confidence_threshold=0.5,
    )
    aligned_df = hmm.align_macro_data(
        daily_df,
        macro_df,
        release_lag=5,  # 模拟 5 天发布滞后
        max_fill_days=90,
    )
    print(f"  - 对齐后数据：{len(aligned_df)} 行，{len(aligned_df.columns)} 列")
    print(f"  - 新增列：{aligned_df.columns[-2:].tolist()}（宏观数据 + 新鲜度）")

    # ============ 步骤 3：训练 HMM 模型 ============
    print("\n[3/5] 训练 HMM 模型...")
    hmm.fit(
        aligned_df,
        feature_columns=["yield_rate", "exchange_rate", "pmi", "cpi"],
        drop_na=True,
    )

    # ============ 步骤 4：预测状态概率矩阵 ============
    print("\n[4/5] 预测状态概率矩阵...")
    prob_matrix, entropy = hmm.predict(aligned_df)
    print(f"  - 概率矩阵：{prob_matrix.shape}")
    print(f"  - 前 5 天概率：\n{prob_matrix.head()}")
    print(f"  - 前 5 天不确定性（熵）：\n{entropy.head()}")

    # ============ 步骤 5：预测置信度 ============
    print("\n[5/5] 预测置信度...")
    prob_matrix, entropy, confidence = hmm.predict_with_confidence(aligned_df)
    print(f"  - 置信度统计：")
    print(f"    - 高置信度天数（≥50%）：{confidence.sum()} 天")
    print(f"    - 低置信度天数（<50%）：{(confidence == 0).sum()} 天")
    print(f"  - 平均熵值（不确定性）：{entropy.mean():.4f}")

    # ============ 输出状态标签 ============
    print("\n状态标签：")
    state_labels = hmm.get_state_labels()
    for state_id, label in state_labels.items():
        print(f"  - 状态 {state_id}：{label}")

    # ============ 完成 ============
    print("\n" + "=" * 60)
    print("测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    test_hmm_macro_module()