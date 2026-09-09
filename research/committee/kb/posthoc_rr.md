语料 rr 列=已实现 R 倍数（事后变量）——09-06 定谳（源码级）：
- strategies/neckline/strategy.py:188：逐笔 rr = avg_pnl_pct / risk_pct = 已实现盈亏÷风险百分比，是出场后才知道的结果变量；
- 实证指纹：语料 rr≥1 桶 12,142 笔胜率 100%（全 tp2）、rr<1 占 82% 且包揽全部 26,811 笔止损——任何基于语料 rr 的分层/过滤/sizing 都是幸存者偏差（09-06 委员会 exec 分析师曾据此提「rr 条件化 sizing」，被当场拦下）；
- 信号时几何 rr=(tp2−entry)/(entry−stop) 在识别层 min_rr=2.0 把守，与语料 rr 是两个量；
- 若需信号时 rr 特征：可由语料 neckline/bottom/atr/entry_price 列几何复算（tp2=颈线+tp_h_mult×H，H=颈线−bottom，stop=颈线−stop_atr_mult×ATR）。
