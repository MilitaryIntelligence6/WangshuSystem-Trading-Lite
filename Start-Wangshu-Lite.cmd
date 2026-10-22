@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.13 -m venv .venv
    if errorlevel 1 goto fail
)
if not exist ".venv\lite-installed-v2" (
    ".venv\Scripts\python.exe" -m pip install -e ".[desktop,ctp,strategy]"
    if errorlevel 1 goto fail
    echo installed>".venv\lite-installed-v2"
)
".venv\Scripts\python.exe" -m wangshu_lite
if errorlevel 1 goto fail
exit /b 0
:fail
echo Wangshu Lite could not start. Read the error above.
pause
exit /b 1
