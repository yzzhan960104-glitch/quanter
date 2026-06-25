"""交互式图表（Plotly）

职责：
1. 生成交互式净值曲线
2. 生成回撤图
3. 生成因子相关性热力图
4. 支持 Jupyter Notebook 探索

设计原则：
- 纯 Plotly 实现（无黑盒）
- 交互式（缩放、平移）
- 支持导出为 HTML
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from typing import Dict, Any, Optional


class InteractiveChart:
    """
    交互式图表生成器

    支持的图表类型：
    1. 净值曲线（含回撤）
    2. 滚动指标（夏普、波动率）
    3. 信号与价格对比
    4. 因子相关性热力图
    """

    def __init__(self, theme: str = "plotly_white"):
        """
        初始化图表生成器

        参数：
            theme: 图表主题
        """
        self.theme = theme

    def plot_nav_curve(
        self,
        daily_df: pd.DataFrame,
        title: str = "净值曲线与最大回撤",
        show: bool = True
    ) -> go.Figure:
        """
        绘制净值曲线与回撤

        参数：
            daily_df: 每日记录 DataFrame（需包含 'nav' 列）
            title: 图表标题
            show: 是否显示图表

        返回：
            Plotly Figure 对象
        """
        # 计算累计收益率
        cumulative = (daily_df["nav"] / daily_df["nav"].iloc[0] - 1) * 100

        # 计算回撤
        rolling_max = daily_df["nav"].expanding().max()
        drawdown = ((daily_df["nav"] - rolling_max) / rolling_max) * 100

        # 创建子图
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.05,
            row_heights=[0.7, 0.3],
            subplot_titles=("净值曲线", "最大回撤")
        )

        # 添加净值曲线
        fig.add_trace(
            go.Scatter(
                x=daily_df.index,
                y=cumulative,
                mode="lines",
                name="累计收益率",
                line=dict(color="blue", width=2),
            ),
            row=1, col=1
        )

        # 添加回撤曲线
        fig.add_trace(
            go.Scatter(
                x=daily_df.index,
                y=drawdown,
                mode="lines",
                name="最大回撤",
                line=dict(color="red", width=1),
                fill="tozeroy",
                fillcolor="rgba(255, 0, 0, 0.3)",
            ),
            row=2, col=1
        )

        # 更新布局
        fig.update_layout(
            title=title,
            template=self.theme,
            hovermode="x unified",
        )

        # 更新 Y 轴标签
        fig.update_yaxes(title_text="收益率 (%)", row=1, col=1)
        fig.update_yaxes(title_text="回撤 (%)", row=2, col=1)

        if show:
            fig.show()

        return fig

    def plot_rolling_metrics(
        self,
        rolling_df: pd.DataFrame,
        title: str = "滚动指标",
        show: bool = True
    ) -> go.Figure:
        """
        绘制滚动指标

        参数：
            rolling_df: 滚动指标 DataFrame
            title: 图表标题
            show: 是否显示图表

        返回：
            Plotly Figure 对象
        """
        # 创建子图
        fig = make_subplots(
            rows=3, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.05,
            subplot_titles=("滚动收益率", "滚动波动率", "滚动夏普比率")
        )

        # 添加滚动收益率
        fig.add_trace(
            go.Scatter(
                x=rolling_df.index,
                y=rolling_df["rolling_return"] * 100,
                mode="lines",
                name="滚动收益率",
                line=dict(color="blue", width=1),
            ),
            row=1, col=1
        )

        # 添加滚动波动率
        fig.add_trace(
            go.Scatter(
                x=rolling_df.index,
                y=rolling_df["rolling_volatility"] * 100,
                mode="lines",
                name="滚动波动率",
                line=dict(color="orange", width=1),
            ),
            row=2, col=1
        )

        # 添加滚动夏普比率
        fig.add_trace(
            go.Scatter(
                x=rolling_df.index,
                y=rolling_df["rolling_sharpe"],
                mode="lines",
                name="滚动夏普比率",
                line=dict(color="green", width=1),
            ),
            row=3, col=1
        )

        # 更新布局
        fig.update_layout(
            title=title,
            template=self.theme,
            hovermode="x unified",
        )

        # 更新 Y 轴标签
        fig.update_yaxes(title_text="收益率 (%)", row=1, col=1)
        fig.update_yaxes(title_text="波动率 (%)", row=2, col=1)
        fig.update_yaxes(title_text="夏普比率", row=3, col=1)

        if show:
            fig.show()

        return fig

    def plot_signal_vs_price(
        self,
        df: pd.DataFrame,
        signal: pd.Series,
        title: str = "信号与价格对比",
        show: bool = True
    ) -> go.Figure:
        """
        绘制信号与价格对比图

        参数：
            df: OHLCV 数据
            signal: 信号序列
            title: 图表标题
            show: 是否显示图表

        返回：
            Plotly Figure 对象
        """
        # 创建子图
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.05,
            subplot_titles=("价格", "信号"),
            row_heights=[0.6, 0.4],
        )

        # 添加价格曲线
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["close"],
                mode="lines",
                name="收盘价",
                line=dict(color="blue", width=1),
            ),
            row=1, col=1
        )

        # 添加信号曲线
        fig.add_trace(
            go.Scatter(
                x=signal.index,
                y=signal,
                mode="lines",
                name="信号",
                line=dict(color="red", width=1),
            ),
            row=2, col=1
        )

        # 添加信号阈值线
        fig.add_hline(y=0.5, line_dash="dash", line_color="gray", row=2, col=1)

        # 更新布局
        fig.update_layout(
            title=title,
            template=self.theme,
            hovermode="x unified",
        )

        # 更新 Y 轴标签
        fig.update_yaxes(title_text="价格", row=1, col=1)
        fig.update_yaxes(title_text="信号", row=2, col=1)

        if show:
            fig.show()

        return fig

    def plot_factor_correlation(
        self,
        factor_attribution: Dict[str, Any],
        title: str = "因子滚动相关性",
        show: bool = True
    ) -> go.Figure:
        """
        绘制因子滚动相关性

        参数：
            factor_attribution: 因子归因字典
            title: 图表标题
            show: 是否显示图表

        返回：
            Plotly Figure 对象
        """
        fig = go.Figure()

        # 添加技术信号滚动相关性
        fig.add_trace(
            go.Scatter(
                x=factor_attribution["tech_rolling_corr"].index,
                y=factor_attribution["tech_rolling_corr"],
                mode="lines",
                name="技术信号",
                line=dict(color="blue", width=1),
            )
        )

        # 添加宏观信号滚动相关性
        fig.add_trace(
            go.Scatter(
                x=factor_attribution["macro_rolling_corr"].index,
                y=factor_attribution["macro_rolling_corr"],
                mode="lines",
                name="宏观信号",
                line=dict(color="orange", width=1),
            )
        )

        # 添加融合信号滚动相关性
        fig.add_trace(
            go.Scatter(
                x=factor_attribution["fused_rolling_corr"].index,
                y=factor_attribution["fused_rolling_corr"],
                mode="lines",
                name="融合信号",
                line=dict(color="green", width=2),
            )
        )

        # 更新布局
        fig.update_layout(
            title=title,
            template=self.theme,
            xaxis_title="日期",
            yaxis_title="相关性",
            hovermode="x unified",
        )

        if show:
            fig.show()

        return fig

    def plot_heatmap(
        self,
        df: pd.DataFrame,
        title: str = "因子相关性热力图",
        show: bool = True
    ) -> go.Figure:
        """
        绘制相关性热力图

        参数：
            df: 数据 DataFrame
            title: 图表标题
            show: 是否显示图表

        返回：
            Plotly Figure 对象
        """
        # 计算相关性矩阵
        corr_matrix = df.corr()

        # 绘制热力图
        fig = px.imshow(
            corr_matrix,
            labels=dict(color="相关性"),
            x=corr_matrix.columns,
            y=corr_matrix.columns,
            color_continuous_scale="RdBu_r",
            title=title,
            aspect="auto",
        )

        # 更新布局
        fig.update_layout(
            template=self.theme,
        )

        # 添加数值标注
        for i, row in enumerate(corr_matrix.values):
            for j, value in enumerate(row):
                fig.add_annotation(
                    text=f"{value:.2f}",
                    x=j,
                    y=i,
                    showarrow=False,
                    font=dict(color="black" if abs(value) < 0.5 else "white"),
                )

        if show:
            fig.show()

        return fig

    def save_html(self, fig: go.Figure, filepath: str):
        """
        保存图表为 HTML

        参数：
            fig: Plotly Figure 对象
            filepath: 文件路径
        """
        fig.write_html(filepath)
        print(f"图表已保存至: {filepath}")