@echo off
cd /d "%~dp0"
python mm2_values.py
if errorlevel 1 pause
