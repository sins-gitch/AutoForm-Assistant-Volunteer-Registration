@echo off
chcp 65001>nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto fail
%PY% -m playwright install chromium
if errorlevel 1 goto fail
echo.
echo 安装完成！请双击 启动助手.bat
pause
exit /b 0
:fail
echo 安装失败：请检查 Python 3.10+ 和网络连接。
pause
exit /b 1
