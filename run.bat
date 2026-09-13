@echo off
setlocal
cd /d "%~dp0"

REM Find an interpreter that actually runs. Note that "where python" is not
REM enough on Windows: with Python not installed there is still a stub on PATH
REM that only opens the Microsoft Store, so we test by executing something.
set "PY="
for %%C in (py python) do (
  if not defined PY (
    %%C -c "import sys" >nul 2>nul && set "PY=%%C"
  )
)
if not defined PY goto :nopython

REM Right version, and with the GUI library present? Both are optional parts of
REM a Python install, so check rather than assume.
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 3)" >nul 2>nul
if errorlevel 3 goto :oldpython
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :notk

REM Everything checks out, so launch without a console window behind it.
for %%C in (pythonw pyw) do (
  %%C -c "import sys" >nul 2>nul && (start "" %%C app.py & goto :eof)
)
%PY% app.py
goto :eof

:nopython
echo.
echo   Python isn't installed, so this app can't run.
echo.
echo   Get it from  https://www.python.org/downloads/
echo   On the first screen of the installer, tick "Add python.exe to PATH".
echo.
echo   Then run this file again.
echo.
pause
goto :eof

:oldpython
echo.
echo   Your Python is too old - this needs 3.9 or newer.
for /f "delims=" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])"') do echo   You have %%V
echo.
echo   Get a newer one from  https://www.python.org/downloads/
echo.
pause
goto :eof

:notk
echo.
echo   Python is installed but tkinter is missing, so there's no window to show.
echo.
echo   Re-run the Python installer, choose "Modify", and make sure
echo   "tcl/tk and IDLE" is ticked.
echo.
pause
goto :eof
