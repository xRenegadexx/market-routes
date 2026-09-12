@echo off
REM Launches the window with no console behind it.
REM If pythonw isn't on PATH, fall back to python so errors stay visible.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw app.py) || (python app.py)
