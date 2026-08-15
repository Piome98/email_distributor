@echo off
rem ---------------------------------------------------------------
rem  ASCII ONLY.
rem  cmd.exe parses a .bat with the console codepage, so a multi-byte
rem  character shifts the parser and splits later tokens - "echo"
rem  becomes "ho". Korean text belongs in the Python layer, which
rem  handles UTF-8 properly. Do not add non-ASCII characters here.
rem ---------------------------------------------------------------
setlocal
pushd "%~dp0"

echo ================================================================
echo   Email Distributor - Setup
echo ================================================================
echo.

call :find_python
if errorlevel 1 goto :no_python

echo [1/3] Python found (%PYDESC%)
%PYRUN% --version
echo.

echo [2/3] Installing pywin32, the only dependency.
echo       "--user" means no administrator rights are needed.
echo.
%PYRUN% -m pip install --user --upgrade pywin32
if errorlevel 1 goto :pip_failed
echo.

echo [3/3] Checking that Outlook can be reached...
%PYRUN% -c "import win32com.client as w; n=w.Dispatch('Outlook.Application').GetNamespace('MAPI'); print('      OK - Outlook reachable, inbox holds', n.GetDefaultFolder(6).Items.Count, 'messages')"
if errorlevel 1 goto :no_outlook

echo.
echo ================================================================
echo   Setup complete.
echo.
echo   Next steps, in order:
echo     1. learn.bat     read the mailbox, build the database
echo     2. start.bat     open the app, review the Companies tab
echo     3. preview.bat   see what would be filed (changes nothing)
echo ================================================================
goto :end

:find_python
set "PYRUN="
set "PYDESC="
where py >nul 2>&1 && (set "PYRUN=py -3" & set "PYDESC=py launcher")
if not defined PYRUN where python >nul 2>&1 && (set "PYRUN=python" & set "PYDESC=python on PATH")
if not defined PYRUN exit /b 1
exit /b 0

:no_python
echo [X] Python was not found on this PC.
echo.
echo     Install Python 3.10 or newer from
echo       https://www.python.org/downloads/windows/
echo.
echo     IMPORTANT: tick "Add python.exe to PATH" during installation.
echo     Administrator rights are not needed - choose "Install for me only".
goto :end

:pip_failed
echo.
echo [X] pip could not install pywin32.
echo     If this PC blocks PyPI, ask IT for the pywin32 wheel file and run:
echo       python -m pip install --user pywin32-XXX-win_amd64.whl
goto :end

:no_outlook
echo.
echo [X] Could not reach Outlook.
echo     Make sure the classic Outlook desktop client is installed and
echo     opens normally, then run this again.

:end
echo.
popd
pause
