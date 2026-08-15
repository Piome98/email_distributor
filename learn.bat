@echo off
rem ASCII ONLY - see the note in setup.bat.
chcp 65001 >nul
setlocal
pushd "%~dp0"
set PYTHONIOENCODING=utf-8

echo ================================================================
echo   Reading your mailbox to build the company/contact database
echo.
echo   This only READS your mail.
echo   Nothing is moved, changed or deleted.
echo ================================================================
echo.

call :find_python
if errorlevel 1 goto :no_python

%PYRUN% "%~dp0cli.py" learn %*

echo.
echo ----------------------------------------------------------------
echo  Check the summary line above:
echo    "Exchange sender(s) resolved"   - good, real addresses found
echo    a WARNING about unresolved      - stop and report it
echo.
echo  Next: start.bat, then review the Companies tab before filing.
echo ----------------------------------------------------------------
goto :end

:find_python
set "PYRUN="
where py >nul 2>&1 && set "PYRUN=py -3"
if not defined PYRUN where python >nul 2>&1 && set "PYRUN=python"
if not defined PYRUN exit /b 1
exit /b 0

:no_python
echo [X] Python was not found. Run setup.bat first.

:end
echo.
popd
pause
