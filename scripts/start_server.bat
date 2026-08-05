@echo off
chcp 65001 >nul
REM ============ C-7 V4：schtasks ONSTART 入口（QuanterServer） ============
REM 物理意图（spec §3.4）：替代旧 ops/start_all.py + scripts/start_all.bat 的
REM   「subprocess.Popen DETACHED uvicorn + 启动文件夹 ONLOGON」启动链。
REM   由 schtasks /SC ONSTART 包裹成开机 session 0 后台进程（不依赖用户登录/logoff，
REM   适配生产机不 7x24）。本 bat 仅「cd + python -m trading」，无 detach 代码——
REM   schtasks 已负责后台化；无 ONLOGON 快捷式自安装——开机自启改由 schtasks ONSTART。
REM
REM 注册：python -m ops.manage_ops_schtasks --register-server --user <用户>
REM   （/RP 密码手动输，见 register_server 提示）
REM
REM Why python -m trading 而非 uvicorn：C-5 V1 已让 __main__.run_server 起 uvicorn
REM   :8000（端口单例 + lifespan 装 engine/connect/discovery）。直接 -m trading = 单进程
REM   启动链（消除历史双进程抢 QMT session，[[qmt-connect-1-rootcause]] 教训）。
cd /d "F:\quanter"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
REM A5（P1-2）：生产链 fail-closed——非 live 一律拒绝（防 dry_run 实例接管生产端口/日志）
set QUANTER_REQUIRE_LIVE=1
".venv310\Scripts\python.exe" -m trading
