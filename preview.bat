@echo off
rem ASCII ONLY - see the note in setup.bat.
chcp 65001 >nul
setlocal
pushd "%~dp0"
set PYTHONIOENCODING=utf-8

echo ================================================================
echo   PREVIEW - DRY RUN
echo.
echo   Shows exactly what WOULD happen to each message.
echo   Nothing in your mailbox is moved or changed.
echo ================================================================
echo.

call :find_python
if errorlevel 1 goto :no_python

%PYRUN% "%~dp0cli.py" run %*
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
