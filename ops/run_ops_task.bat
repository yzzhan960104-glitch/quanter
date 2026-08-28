@echo off
REM ============================================================================
REM run_ops_task.bat —— 掘金运维 schtasks 统一入口（W0，2026-08-28 全库评审 P0-2）
REM
REM Why：schtasks /TR 裸 python 时 Task Scheduler 不捕获 stdout/stderr，脚本启动期
REM      崩溃（import 错/路径错，尚未走到 notify）的 traceback 无任何痕迹=双重静默
REM      的第三层。本包装器统一重定向到 logs\ops_schtask.log（先例：run_audit.bat）。
REM 用法：schtasks /TR "E:\quanter\ops\run_ops_task.bat <script_name>.py"
REM      退出码透传（python 失败 → Task Scheduler 上次运行结果非 0，晨检可见）。
REM ============================================================================
setlocal
set "PY=%~dp0..\.venv310\Scripts\python.exe"
set "LOG=%~dp0..\logs\ops_schtask.log"
if not exist "%~dp0..\logs" mkdir "%~dp0..\logs"
echo [%date% %time%] ==== %1 begin ==== >> "%LOG%"
"%PY%" "%~dp0%~1" %2 %3 %4 %5 >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] ==== %1 exit %RC% ==== >> "%LOG%"
exit /b %RC%
