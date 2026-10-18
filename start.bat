@echo off
rem Agent Dashboard launcher (Windows) - double click to start
rem (ASCII only on purpose: cmd.exe misreads UTF-8 Japanese text in .bat files)
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% -m agentdash %*
if errorlevel 1 pause
