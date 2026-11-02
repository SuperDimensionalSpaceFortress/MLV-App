@echo off
rem Test stub shim, the same shape as the installed heavy-slot.cmd: it runs heavy-slot.ps1 beside this file.
pwsh -NoProfile -NonInteractive -File "%~dp0heavy-slot.ps1" %*
exit /b %ERRORLEVEL%
