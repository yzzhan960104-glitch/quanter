@echo off
:: scripts/run_data_check_t2.bat
:: 数据检查点②（盘后 18:30 查 T）schtasks 入口（auto-trading-rehearsal Task 4）
:: 单次触发：重采窗口（每 15min 重采至 20:00 deadline）由 run_data_check 进程内 sleep 控制。
:: 仍 FAIL → 熔断 eod_plan（不交易不自欺，绝不用 T-1 兜底算 T+1＝前视偏差）。
cd /d C:\Users\yzzhan\Desktop\quanter
call .venv310\Scripts\activate.bat
python -m scripts.run_data_check t2
