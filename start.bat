@echo off
rem Opens the desktop app. Uses pythonw so no console window appears.
setlocal
pushd "%~dp0"

set "PYW="
where pyw >nul 2>&1 && set "PYW=pyw -3"
if not defined PYW where py >nul 2>&1 && set "PYW=py -3w"
if not defined PYW where pythonw >nul 2>&1 && set "PYW=pythonw"

if not defined PYW (
    echo Python was not found. Run setup.bat first.
    popd
    pause
    exit /b 1
)

start "" %PYW% "%~dp0run.pyw"
popd
