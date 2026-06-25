# -*- coding: utf-8 -*-
"""
量化交易系统示例演示

本示例展示如何使用量化交易系统：
1. 获取模拟数据
2. 清洗数据
3. 计算因子（技术信号 + 宏观信号）
4. 融合信号
5. 执行回测
6. 生成可视化图表
7. 生成报告
"""

import sys
from datetime import datetime, timedelta
import pandas as pd

# 导入系统模块
from data import MockDataFetcher, DataCleaner
from factors import moving_average_cross, volume_price_trend, macro_anchor_signal, signal_fusion
from backtest import BacktestEngine, CostModel
from viz import InteractiveChart, ReportGenerator


def main():
    """主函数：演示完整流程"""
    print("=" * 60)
    print("量化交易系统演示")
    print("=" * 60)

    # 配置参数
    start_date = datetime(2023, 1, 1)
    end_date = datetime(2024, 12, 31)
    symbol = "600000.SH"

    # ==================== 第 1 步：获取数据 ====================
    print("\n[1/7] 获取数据...")
    fetcher = MockDataFetcher(seed=42)

    # 获取 OHLCV 数据
    df = fetcher.fetch_ohlcv(symbol, start_date, end_date, freq="1d")
    print(f"  - 获取 {len(df)} 个交易日的 OHLCV 数据")

    # 获取宏观数据
    macro_df = fetcher.fetch_macro("m2", start_date, end_date)
    print(f"  - 获取 {len(macro_df)} 个月的宏观数据")

    # ==================== 第 2 步：清洗数据 ====================
    print("\n[2/7] 清洗数据...")
    cleaner = DataCleaner()

    # 清洗 OHLCV 数据
    df_clean = cleaner.clean_ohlcv(df, max_fill=5)
    print(f"  - 清洗后数据量: {len(df_clean)}")

    # 对齐多频率数据（使用 bfill 填充宏观数据）
    try:
        df_aligned = cleaner.align_frequencies(df_clean, macro_df)
    except ValueError as e:
        # 如果宏观数据不完整，使用纯技术信号
        print(f"  - 宏观数据对齐失败，使用纯技术信号: {e}")
        df_aligned = df_clean.copy()
        # 添加虚拟宏观列
        df_aligned["m2"] = 200.0

    print(f"  - 对齐后数据量: {len(df_aligned)}")

    # 验证数据质量
    quality_report = cleaner.validate_data(df_aligned)
    print(f"  - 数据质量报告: {quality_report}")

    # ==================== 第 3 步：计算技术信号 ====================
    print("\n[3/7] 计算技术信号...")

    # 计算双均线信号
    ma_signal = moving_average_cross(df_aligned, short_window=5, long_window=20)
    print(f"  - 双均线信号: 范围 [{ma_signal.min():.2f}, {ma_signal.max():.2f}]")

    # 计算量价趋势信号
    vpt_signal = volume_price_trend(df_aligned, window=20)
    print(f"  - VPT 信号: 范围 [{vpt_signal.min():.2f}, {vpt_signal.max():.2f}]")

    # 技术信号融合（简单平均）
    tech_signal = (ma_signal + vpt_signal) / 2
    print(f"  - 融合技术信号: 范围 [{tech_signal.min():.2f}, {tech_signal.max():.2f}]")

    # ==================== 第 4 步：计算宏观信号 ====================
    print("\n[4/7] 计算宏观信号...")

    # 计算宏观锚点信号
    try:
        macro_signal = macro_anchor_signal(macro_df, indicator="m2", threshold=0.02, window=3)
        print(f"  - 宏观信号: 范围 [{macro_signal.min():.2f}, {macro_signal.max():.2f}]")
    except Exception as e:
        # 如果宏观信号计算失败，使用中等多头信号
        print(f"  - 宏观信号计算失败，使用默认信号: {e}")
        macro_signal = pd.Series(0.5, index=tech_signal.index)

    # ==================== 第 5 步：信号融合 ====================
    print("\n[5/7] 信号融合...")

    # 对齐信号索引
    aligned_index = tech_signal.index.intersection(macro_signal.index)
    tech_aligned = tech_signal.loc[aligned_index]
    macro_aligned = macro_signal.loc[aligned_index]

    # 融合技术信号与宏观信号
    fused_signal = signal_fusion(
        tech_aligned,
        macro_aligned,
        weights={"tech": 0.7, "macro": 0.3}
    )
    print(f"  - 融合信号: 范围 [{fused_signal.min():.2f}, {fused_signal.max():.2f}]")

    # ==================== 第 6 步：执行回测 ====================
    print("\n[6/7] 执行回测...")

    # 初始化成本模型
    cost_model = CostModel(
        commission_rate=0.0003,
        stamp_duty=0.0005,
        min_commission=5.0,
        slippage_model="linear",
        slippage_rate=0.001,
        liquidity_threshold=0.02,
    )

    # 初始化回测引擎
    engine = BacktestEngine(
        initial_capital=1_000_000,
        cost_model=cost_model,
        signal_freq="1d"
    )

    # 执行回测（使用对齐后的数据）
    df_for_backtest = df_aligned.loc[fused_signal.index].copy()
    result = engine.run(df_for_backtest, fused_signal, symbol=symbol)

    # 输出回测结果
    print(f"\n  【回测结果】")
    print(f"  - 初始资金: {result['initial_capital']:,.2f} 元")
    print(f"  - 最终净值: {result['final_nav']:,.2f} 元")
    print(f"  - 累计收益: {result['total_return']:.2%}")
    print(f"  - 年化收益: {result['annual_return']:.2%}")
    print(f"  - 最大回撤: {result['max_drawdown']:.2%}")
    print(f"  - 夏普比率: {result['sharpe_ratio']:.2f}")
    print(f"  - 卡玛比率: {result['calmar_ratio']:.2f}")
    print(f"  - 胜率: {result['win_rate']:.2%}")
    print(f"  - 交易次数: {result['n_trades']}")
    print(f"  - 失败交易次数: {result['n_failed_trades']}")

    # ==================== 第 7 步：生成可视化 ====================
    print("\n[7/7] 生成可视化...")

    # 初始化图表生成器
    chart_generator = InteractiveChart(theme="plotly_white")

    # 生成净值曲线
    nav_fig = chart_generator.plot_nav_curve(
        result["daily_records"],
        title="净值曲线与最大回撤",
        show=False
    )
    chart_generator.save_html(nav_fig, "reports/nav_curve.html")
    print("  - 净值曲线已保存至: reports/nav_curve.html")

    # 生成报告
    print("\n生成报告...")
    report_generator = ReportGenerator()
    report_generator.generate_html_report(
        result,
        "reports/backtest_report.html",
        include_charts=False
    )
    print("  - 报告已保存至: reports/backtest_report.html")

    print("\n" + "=" * 60)
    print("演示完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()