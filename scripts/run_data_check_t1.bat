@echo off
:: scripts/run_data_check_t1.bat
:: 数据检查点①（盘前 17:00 查 T-1）schtasks 入口（auto-trading-rehearsal Task 4）
:: FAIL 仅告警，不熔断（T-1 缺不影响当日 T+1 计划的 T 日数据输入）。
cd /d C:\Users\yzzhan\Desktop\quanter
call .venv310\Scripts\activate.bat
python -m scripts.run_data_check t1
