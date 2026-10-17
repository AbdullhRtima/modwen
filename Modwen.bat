@echo off
rem Modwen launcher for Windows. Double-click. First run sets everything up (a few minutes).
rem Close this window to quit Modwen.
setlocal
cd /d "%~dp0"
echo Modwen
echo.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  echo Installing uv ^(manages Modwen's private Python; nothing else on your system changes^)...
  powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >nul 2>nul || (echo Could not install uv. Check your internet connection. & pause & exit /b 1)

set "EXTRAS=--extra stt --extra desktop --extra windows"
where nvidia-smi >nul 2>nul && set "EXTRAS=%EXTRAS% --extra cuda"

echo Checking Modwen's environment...
uv sync --frozen --no-dev %EXTRAS%
if errorlevel 1 (echo. & echo Setup failed ^(see above^). & pause & exit /b 1)

if not exist ui\dist\index.html (
  where npm >nul 2>nul && (pushd ui & call npm ci --no-audit --no-fund & call npm run build & popd) || echo Note: the web UI is not built. Use a release zip, or install Node.js.
)

echo.
echo Starting Modwen... your browser will open. Keep this window open; close it to quit.
uv run --frozen --no-dev modwen serve
pause
