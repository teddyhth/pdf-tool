@echo off
REM Shared by the launch / build scripts (called with CALL; caller already ran chcp 65001).
REM Finds a Python with tkinter, creates .venv-win and installs requirements.
REM On success VENV_PY points to the venv python and errorlevel is 0.

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "VENV=%ROOT%\.venv-win"
set "VENV_PY=%VENV%\Scripts\python.exe"

if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import tkinter" >nul 2>nul && goto :deps
  rmdir /s /q "%VENV%"
)

echo 第一次使用，正在建立執行環境…
call :find_python
if defined BASE_PY goto :make_venv

where winget >nul 2>nul
if errorlevel 1 goto :no_python
echo 找不到 Python，正在用 winget 自動安裝 Python 3.12…
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :find_python
if defined BASE_PY goto :make_venv

:no_python
start "" "https://www.python.org/downloads/windows/"
echo 找不到可用的 Python。請從開啟的網頁下載安裝 Python 3.12，
echo 安裝時記得勾選 Add python.exe to PATH，裝好後再雙擊一次。
exit /b 1

:make_venv
echo 使用 Python：%BASE_PY%
"%BASE_PY%" -m venv "%VENV%"
if errorlevel 1 (
  echo 建立虛擬環境失敗
  exit /b 1
)

:deps
REM requirements.txt 有變動才重新安裝
fc /b "%ROOT%\requirements.txt" "%VENV%\installed-requirements.txt" >nul 2>nul && exit /b 0
echo 正在安裝相依套件（只需要一次，約 1 分鐘）…
"%VENV_PY%" -m pip install --disable-pip-version-check -q -r "%ROOT%\requirements.txt"
if errorlevel 1 (
  echo 安裝相依套件失敗，請確認網路連線
  exit /b 1
)
copy /y "%ROOT%\requirements.txt" "%VENV%\installed-requirements.txt" >nul
exit /b 0

:find_python
REM Microsoft Store 的 python.exe 捷徑不會印出路徑，因此會被自動略過
set "BASE_PY="
for /f "delims=" %%P in ('py -3 -c "import sys;import tkinter;print(sys.executable)" 2^>nul') do set "BASE_PY=%%P"
if defined BASE_PY exit /b 0
for /f "delims=" %%P in ('python -c "import sys;import tkinter;print(sys.executable)" 2^>nul') do set "BASE_PY=%%P"
if defined BASE_PY exit /b 0
for %%V in (312 313 311 314) do (
  if not defined BASE_PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "BASE_PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
exit /b 0
