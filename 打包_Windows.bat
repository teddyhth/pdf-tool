@echo off
chcp 65001 >nul
REM 雙擊打包成單一執行檔 dist\PDFTool.exe（之後可直接雙擊 exe）
cd /d "%~dp0"
call tools\setup_win.bat
if errorlevel 1 goto :error

echo 正在安裝 PyInstaller…
"%VENV_PY%" -m pip install --disable-pip-version-check -q pyinstaller
if errorlevel 1 goto :error

echo 正在打包（約 1–2 分鐘）…
"%VENV_PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --log-level WARN --name PDFTool --distpath "%ROOT%\dist" --workpath "%ROOT%\build" --specpath "%ROOT%\build" "%ROOT%\main.py"
if errorlevel 1 goto :error

echo.
echo 完成：dist\PDFTool.exe
explorer "%ROOT%\dist"
pause
exit /b 0

:error
echo.
echo 失敗，請將上方訊息截圖回報。
pause
exit /b 1
