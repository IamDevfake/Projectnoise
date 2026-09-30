@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% --version >nul 2>nul || (echo ไม่พบ Python - ติดตั้งจาก https://www.python.org/downloads/ แล้วติ๊ก "Add python.exe to PATH" & pause & exit /b 1)
%PY% -c "import numpy" >nul 2>nul || %PY% -m pip install --user -r requirements.txt
%PY% denoise_gui.py
if errorlevel 1 pause