@echo off
rem Runs the Instagram part on the home PC. Task Scheduler starts this file.
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONUTF8=1
if exist .git git pull -q --ff-only >> logs\instagram.log 2>&1
".venv\Scripts\python.exe" -m bot --platforms instagram >> logs\instagram.log 2>&1
