@echo off
chcp 65001 >nul
cd /d "F:\quanter"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ============ Self-install startup shortcut (idempotent) ============
REM First run of this .bat creates a shortcut in the Startup folder; later logins auto-trigger it.
REM Skipped if the shortcut already exists (idempotent). Remove the .lnk to disable autostart.
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP_DIR%\QuanterStartAll.lnk"
if not exist "%LNK%" (
    powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $l = $ws.CreateShortcut('%LNK%'); $l.TargetPath = 'F:\quanter\scripts\start_all.bat'; $l.WorkingDirectory = 'F:\quanter'; $l.WindowStyle = 7; $l.Save()"
    if exist "%LNK%" (echo [autostart] shortcut created) else (echo [autostart] WARN create failed - manual: Win+R shell:startup then drop this .bat there)
) else (
    echo [autostart] shortcut already exists, skip
)

REM ============ Launch full stack (detached daemons) ============
".venv310\Scripts\python.exe" ops/start_all.py

echo.
echo === Done (daemons run in background, closing this window is safe) ===
echo Logs: logs\uvicorn.log / logs/trading_engine.log / logs\broadcast_connect\
echo Autostart: %LNK% (delete it to disable)
echo.
pause
