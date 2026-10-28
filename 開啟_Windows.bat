@echo off
chcp 65001 >nul
REM 雙擊開啟 PDF 工具（第一次會自動安裝所需套件）
cd /d "%~dp0"
call tools\setup_win.bat
if errorlevel 1 goto :error

REM pythonw 不會顯示黑色主控台視窗；啟動後這個視窗會自動關閉
start "" "%VENV%\Scripts\pythonw.exe" "%ROOT%\main.py"
exit /b 0

:error
echo.
echo 安裝失敗，請將上方訊息截圖回報。
pause
exit /b 1
