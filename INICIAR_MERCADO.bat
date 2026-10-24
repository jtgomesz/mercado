@echo off
setlocal
set "APP_DIR=%~dp0mercado"
where py >nul 2>nul
if %errorlevel%==0 (
  start "Mercado Pro - servidor" /D "%APP_DIR%" cmd /k "py -3 app.py"
) else (
  start "Mercado Pro - servidor" /D "%APP_DIR%" cmd /k "python app.py"
)
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8765"
endlocal
