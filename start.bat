@echo off
setlocal EnableExtensions
title A股阶段跟踪研究平台

REM ============================================================
REM  一键启动：首次运行自动创建虚拟环境并安装依赖
REM  用法：双击运行，浏览器会自动打开
REM  停止：关闭本窗口，或按 Ctrl+C
REM ============================================================

cd /d "%~dp0"

REM ---- 1. 检测可用的 Python（优先 py 启动器，排除 Store 占位 stub）----
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys; exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [错误] 未检测到可用的 Python 3.9+。
  echo        常见原因：电脑上只有 Microsoft Store 的 python 占位程序。
  echo        请到 https://www.python.org/downloads/ 安装 Python 3.9 或更高版本，
  echo        安装时务必勾选 "Add python.exe to PATH"，然后重新运行本脚本。
  echo.
  pause
  exit /b 1
)
for /f "tokens=2" %%i in ('%PY% --version 2^>^&1') do set PYVER=%%i
echo [1/4] Python %PYVER% OK

REM ---- 2. 虚拟环境 ----
if not exist .venv\Scripts\python.exe (
  echo [2/4] 首次运行：创建虚拟环境，约 10 秒...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败
    pause
    exit /b 1
  )
) else (
  echo [2/4] 虚拟环境已存在，跳过
)
set "VPY=.venv\Scripts\python.exe"

REM ---- 3. 依赖 ----
if not exist .venv\.deps_installed (
  echo [3/4] 安装依赖，首次约 1-2 分钟，请保持网络畅通...
  "%VPY%" -m pip install --quiet --upgrade pip
  "%VPY%" -m pip install --quiet -r requirements.txt -r requirements-scheduler.txt
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
  )
  type nul > .venv\.deps_installed
) else (
  echo [3/4] 依赖已安装，跳过
)

REM ---- 4. 配置文件与样例数据 ----
if not exist .env (
  copy /y .env.example .env >nul
  echo [4/4] 已从 .env.example 生成 .env，默认配置即可运行；想配 AI 或检索 Key 请看 README 配置说明
) else (
  echo [4/4] .env 已存在，跳过
)
if not exist backend\data\portfolio\holdings.json (
  if not exist backend\data\portfolio mkdir backend\data\portfolio
  copy /y examples\holdings.example.json backend\data\portfolio\holdings.json >nul
  copy /y examples\watchlist.example.json backend\data\portfolio\watchlist.json >nul
  echo        已铺入样例持仓，可在界面里删除后填自己的
)

echo.
echo ============================================================
echo   服务启动中... 地址: http://127.0.0.1:8000/
echo.
echo   首次使用: 先跑一次盘后管道让页面有数据，约 10-20 分钟:
echo     cd backend
echo     ..\.venv\Scripts\python -m jobs.eod
echo.
echo   关闭本窗口即停止服务
echo ============================================================
echo.

start "" http://127.0.0.1:8000/
"%VPY%" -m uvicorn main:app --host 127.0.0.1 --port 8000 --app-dir backend
pause
