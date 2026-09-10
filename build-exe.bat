@echo off
REM Build a standalone asciify-ps.exe with PyInstaller.
REM Result: dist\asciify-ps.exe (single file, no console window, app icon embedded)
setlocal
cd /d "%~dp0"

echo [1/3] Installing build requirements...
python -m pip install --quiet --upgrade pyinstaller || goto :fail

echo [2/3] Building...
python -m PyInstaller --clean --noconfirm asciify-ps.spec || goto :fail

echo [3/3] Done.
echo.
echo   dist\asciify-ps.exe
echo.
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
