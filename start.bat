@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 mm2_values.py %*
) else (
  python mm2_values.py %*
)
if errorlevel 1 pause
