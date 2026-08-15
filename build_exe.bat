@echo off
rem ASCII ONLY - see the note in setup.bat.
rem ---------------------------------------------------------------
rem  Builds a standalone EmailDistributor.exe that carries its own
rem  Python, so the target PC needs nothing installed.
rem
rem  Run this on a machine where you CAN install things, then copy
rem  the resulting dist\EmailDistributor folder to the target PC.
rem
rem  --onedir (a folder), not --onefile (a single file), on purpose:
rem  a one-file build unpacks itself into %TEMP% at every launch, and
rem  managed PCs frequently block executing anything from %TEMP%.
rem  A folder build runs in place and survives that policy.
rem ---------------------------------------------------------------
setlocal
pushd "%~dp0"

echo ================================================================
echo   Building EmailDistributor.exe
echo ================================================================
echo.

call :find_python
if errorlevel 1 goto :no_python

echo [1/3] Installing PyInstaller (build tool, not needed at runtime)...
%PYRUN% -m pip install --user --upgrade pyinstaller
if errorlevel 1 goto :pip_failed
echo.

echo [2/3] Building. This takes a few minutes...
echo.
%PYRUN% -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name EmailDistributor ^
  --paths src ^
  --collect-submodules email_distributor ^
  --hidden-import win32timezone ^
  --hidden-import win32com.client ^
  --hidden-import pythoncom ^
  --hidden-import pywintypes ^
  run.pyw
if errorlevel 1 goto :build_failed
echo.

echo [3/3] Done.
echo.
echo ================================================================
echo   Built: dist\EmailDistributor\EmailDistributor.exe
echo.
echo   Copy the WHOLE "dist\EmailDistributor" folder to the other PC
echo   and run EmailDistributor.exe inside it.
echo.
echo   Note: the .exe is unsigned. SmartScreen will warn on first run
echo   ("More info" -^> "Run anyway"), and some corporate antivirus
echo   quarantines PyInstaller output. If that happens on the office
echo   PC, use the .bat files with Python instead.
echo ================================================================
goto :end

:find_python
set "PYRUN="
where py >nul 2>&1 && set "PYRUN=py -3"
if not defined PYRUN where python >nul 2>&1 && set "PYRUN=python"
if not defined PYRUN exit /b 1
exit /b 0

:no_python
echo [X] Python was not found. Run setup.bat first.
goto :end

:pip_failed
echo [X] Could not install PyInstaller. Check your internet/proxy.
goto :end

:build_failed
echo [X] The build failed. See the messages above.

:end
echo.
popd
pause
