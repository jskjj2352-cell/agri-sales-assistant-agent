@echo off
cd /d "%~dp0"

set PY=
where python >nul 2>&1 && python -c "import sys" >nul 2>&1 && set PY=python
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY where py >nul 2>&1 && set PY=py

if not defined PY (
    echo [ERROR] Python not found. Install from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH".
    echo [ERROR] Python not found > run_log.txt
    pause
    exit /b
)

set PYTHONUTF8=1
%PY% run.py
pause
