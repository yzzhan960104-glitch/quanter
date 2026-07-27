@echo off
chcp 65001 >nul
cd /d "F:\quanter"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ============ 自安装开机自启（幂等 · 首次跑自动配好）============
REM 首次跑本 .bat → 在「启动」文件夹创建快捷方式；之后登录 Windows 自动触发本 .bat。
REM 已存在快捷方式则跳过（幂等）。取消自启 = 删那个 .lnk。
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP_DIR%\QuanterStartAll.lnk"
if not exist "%LNK%" (
    powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $l = $ws.CreateShortcut('%LNK%'); $l.TargetPath = 'F:\quanter\scripts\start_all.bat'; $l.WorkingDirectory = 'F:\quanter'; $l.WindowStyle = 7; $l.Save()"
    if exist "%LNK%" (echo [自安装] 已创建开机自启快捷方式) else (echo [自安装] ⚠️ 创建失败，手动：Win+R shell:startup 拖本 .bat 进去)
) else (
    echo [自安装] 快捷方式已存在，跳过
)

REM ============ 启动全栈（detach 后台常驻）============
".venv310\Scripts\python.exe" ops/start_all.py

echo.
echo === 启动完成（各常驻进程后台运行，关本窗口不影响）===
echo 日志：logs\uvicorn.log / logs\trading_engine.log / logs\broadcast_connect\
echo 开机自启：%LNK%（删它即取消自启）
echo.
pause
