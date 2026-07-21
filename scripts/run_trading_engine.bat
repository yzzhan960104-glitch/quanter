@echo off
REM ============================================================================
REM 二期自动交易引擎常驻启动入口（schtasks / PM2 / Terminal tab 通用）
REM ----------------------------------------------------------------------------
REM 物理意图：Windows 下 schtasks（默认 GBK 控制台） / Git Bash 管道（默认 cp936）
REM 直跑中文日志会出现乱码（Task10 M4 冒烟实测）。生产环境本 bat 由 schtasks 拉起，
REM 必须在 python 进程启动前把三个编码开关固定到 UTF-8，否则 logs/trading_plans/
REM 落盘 + 钉钉推送的中文消息可能出现 ??? / 乱码（事后难排查）。
REM
REM 三件套（缺一不可）：
REM   1. chcp 65001                  —— 切当前 cmd 控制台代码页到 UTF-8（影响 print 中文）
REM   2. PYTHONIOENCODING=utf-8      —— 强制 Python stdio（sys.stdout/stderr）按 UTF-8 编码
REM   3. PYTHONUTF8=1                —— 开启 Python UTF-8 Mode（文件/默认编码全 UTF-8）
REM
REM cwd 锁定 quanter 根目录（一期 3 bot bat 同模式）：Task1 calendar 的 trade_cal
REM 缓存 / trading_plan.py 的 logs/trading_plans 落盘 / __main__.py 的 load_dotenv
REM 都依赖 cwd 在项目根（相对路径语义），schtasks 默认 cwd 是 System32 必须显式 cd /d。
REM
REM 托管方式（三选一，上线时由用户决定，AI/本 bat 不替用户注册 schtasks）：
REM   A. Windows Terminal tab 手动挂着跑（开发/初期影子观测推荐）
REM   B. PM2（pm2 start scripts/run_trading_engine.bat --name trading-engine）
REM   C. schtasks（开机自启 · 影子模式稳定后切生产推荐）
REM ============================================================================
chcp 65001 >nul
cd /d "C:\Users\yzzhan\Desktop\quanter"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv310\Scripts\python.exe" -m trading
