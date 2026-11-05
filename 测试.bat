@echo off
cd /d "%~dp0"
set PY=python
python --version >nul 2>&1
if errorlevel 1 set PY="%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
set PYTHONUTF8=1
%PY% test_tools.py > test_log.txt 2>&1
type test_log.txt
echo.
echo ===== Done. Result saved to test_log.txt =====
pause
