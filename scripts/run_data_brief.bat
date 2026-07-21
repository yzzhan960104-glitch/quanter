@echo off
REM === Data bot daily brief - schtasks entry (Ops Phase-1 Task 7) ===
REM Why cd /d: schtasks default cwd=%WINDIR%\System32. Without it, python -m broadcast
REM fails with ModuleNotFoundError (.env/config/data_lake/broadcast not found).
cd /d "C:\Users\yzzhan\Desktop\quanter"
REM Why .venv310: xtquant binds Python 3.10; broadcast reuses same venv for dep parity.
".venv310\Scripts\python.exe" -m broadcast --bot data
