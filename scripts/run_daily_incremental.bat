@echo off
:: scripts/run_daily_incremental.bat
:: A股日线日频增量同步 schtasks 入口（Phase 1.5 任务3）。
:: @17:30 收盘后拉当天 raw daily + adj_factor 重建前复权 append 到 a_shares_daily.parquet。
::
:: Why 此 bat 必须早于 run_data_check_t2.bat（@18:30）：
::   检查点② 重采 daily 走 sync_daily_incremental（_resync_key 分流），daily 增量调度
::   先跑保证 T 日数据落湖，检查点② 多为一次过 PASS 不进重采熔断窗口（不交易不自欺
::   的熔断只在 daily 增量真失败 + 重采 15min 节流仍 FAIL 时触发）。
::
:: 退出码 0=成功/已最新；1=失败（schtasks 层默认不熔断，仅 Last Run Result 可观测）。
cd /d C:\Users\yzzhan\Desktop\quanter
call .venv310\Scripts\activate.bat
python scripts\sync_daily_incremental.py
