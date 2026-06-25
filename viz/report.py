"""自动化报告生成

职责：
1. 生成 HTML 报告
2. 导出为 PDF（预留）
3. 包含所有关键指标与图表

设计原则：
- 固定格式报告（适合复盘）
- 包含所有关键指标
- 支持导出为 HTML/PDF
"""
import pandas as pd
from typing import Dict, Any, Optional
from datetime import datetime
from .interactive import InteractiveChart
from backtest.metrics import MetricsCalculator


class ReportGenerator:
    """
    报告生成器

    报告内容：
    1. 基本信息（回测期间、初始资金等）
    2. 收益指标（年化收益率、最大回撤等）
    3. 交易指标（胜率、盈亏比等）
    4. 因子归因（技术信号 vs 宏观信号）
    5. 图表（净值曲线、滚动指标等）
    """

    def __init__(self):
        """初始化报告生成器"""
        self.chart_generator = InteractiveChart(theme="plotly_white")

    def generate_html_report(
        self,
        result: Dict[str, Any],
        filepath: str,
        include_charts: bool = True
    ):
        """
        生成 HTML 报告

        参数：
            result: 回测结果字典
            filepath: 文件路径
            include_charts: 是否包含图表
        """
        # 提取数据
        daily_df = result.get("daily_records")
        trades_df = result.get("trades")

        # 计算指标
        if daily_df is not None and len(daily_df) > 0:
            daily_returns = daily_df["nav"].pct_change().dropna()
            return_metrics = MetricsCalculator.calculate_return_metrics(daily_returns)

            if trades_df is not None and len(trades_df) > 0:
                trade_metrics = MetricsCalculator.calculate_trade_metrics(trades_df)
            else:
                trade_metrics = {}
        else:
            return_metrics = {}
            trade_metrics = {}

        # 生成指标报告文本
        metrics_report = MetricsCalculator.generate_metrics_report(
            return_metrics,
            trade_metrics
        )

        # 生成图表 HTML
        charts_html = ""
        if include_charts and daily_df is not None and len(daily_df) > 0:
            # 净值曲线
            nav_fig = self.chart_generator.plot_nav_curve(daily_df, show=False)
            charts_html += f'<div id="nav_chart"></div>'
            self.chart_generator.save_html(nav_fig, filepath.replace(".html", "_nav.html"))

            # 信号与价格对比
            if "signal" in daily_df.columns:
                signal_fig = self.chart_generator.plot_signal_vs_price(
                    daily_df, daily_df["signal"], show=False
                )
                charts_html += f'<div id="signal_chart"></div>'

        # 生成 HTML
        html = self._generate_html_template(
            title="回测报告",
            content=metrics_report,
            charts_html=charts_html,
        )

        # 保存文件
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"报告已保存至: {filepath}")

    def _generate_html_template(
        self,
        title: str,
        content: str,
        charts_html: str
    ) -> str:
        """
        生成 HTML 模板

        参数：
            title: 标题
            content: 内容
            charts_html: 图表 HTML

        返回：
            HTML 字符串
        """
        html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: "Microsoft YaHei", Arial, sans-serif;
            margin: 20px;
            line-height: 1.6;
        }}
        h1 {{
            color: #333;
            border-bottom: 2px solid #333;
            padding-bottom: 10px;
        }}
        h2 {{
            color: #666;
            border-bottom: 1px solid #ccc;
            padding-bottom: 5px;
            margin-top: 30px;
        }}
        pre {{
            background-color: #f5f5f5;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
        }}
        .chart-container {{
            margin: 20px 0;
            border: 1px solid #ddd;
            padding: 10px;
            border-radius: 5px;
        }}
        .footer {{
            margin-top: 50px;
            text-align: center;
            color: #999;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <p>生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>

    <h2>回测指标</h2>
    <pre>{content}</pre>

    {charts_html if charts_html else ""}

    <div class="footer">
        <p>本报告由量化交易系统自动生成</p>
    </div>
</body>
</html>
        """
        return html

    def generate_text_report(
        self,
        result: Dict[str, Any]
    ) -> str:
        """
        生成文本报告

        参数：
            result: 回测结果字典

        返回：
            报告文本
        """
        # 提取数据
        daily_df = result.get("daily_records")
        trades_df = result.get("trades")

        # 计算指标
        if daily_df is not None and len(daily_df) > 0:
            daily_returns = daily_df["nav"].pct_change().dropna()
            return_metrics = MetricsCalculator.calculate_return_metrics(daily_returns)

            if trades_df is not None and len(trades_df) > 0:
                trade_metrics = MetricsCalculator.calculate_trade_metrics(trades_df)
            else:
                trade_metrics = {}
        else:
            return_metrics = {}
            trade_metrics = {}

        # 生成指标报告文本
        return MetricsCalculator.generate_metrics_report(
            return_metrics,
            trade_metrics
        )