@echo off
rem ===================================================================
rem  WinMon - Desktop program live monitor and terminator (launcher)
rem  All Chinese UI text lives inside winmon.py / README.md
rem ===================================================================
setlocal
cd /d "%~dp0"

set "PYW="
set "PYC="
where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
where python.exe  >nul 2>nul && set "PYC=python.exe"
if not defined PYC (
  where py.exe >nul 2>nul && set "PYC=py"
)
if not defined PYW (
  where pyw.exe >nul 2>nul && set "PYW=pyw"
)

if not defined PYC (
  echo.
  echo [ERROR] Python not found. Please install Python 3.9+ first.
  echo         Download: https://www.python.org/downloads/
  echo         Remember to check "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

if not exist "winmon.py" (
  echo.
  echo [ERROR] winmon.py not found in this folder.
  echo.
  pause
  exit /b 1
)

if "%~1"=="--selftest" goto selftest
if "%~1"=="--test" goto selftest

:menu
cls
echo ==========================================================
echo    WinMon - Desktop program live monitor / terminator
echo ==========================================================
echo.
echo     [1] Start WinMon        (open the GUI)
echo     [2] Run self-test       (check capture + terminate)
echo     [3] Open README         (usage instructions)
echo     [4] Exit
echo.
echo ==========================================================
set "CHOICE="
set /p CHOICE=Choose [1/2/3/4]:
if "%CHOICE%"=="2" goto selftest
if "%CHOICE%"=="3" goto readme
if "%CHOICE%"=="4" exit /b 0
if "%CHOICE%"=="1" goto start
goto menu

:start
if defined PYW (
  start "" %PYW% "winmon.py"
) else (
  start "" %PYC% "winmon.py"
)
exit /b 0

:readme
if exist "README.md" (
  start "" "README.md"
) else (
  echo README.md not found.
  pause
)
goto menu

:selftest
echo.
echo Running self-test ...
echo.
%PYC% "winmon.py" --selftest
echo.
pause
goto menu
