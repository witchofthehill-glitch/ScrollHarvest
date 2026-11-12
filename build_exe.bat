@echo off
rem Builds dist\ScrollHarvest.exe - a single file that runs without Python.
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>&1 || python -m pip install --user pyinstaller || goto :fail
python -m pip install --user -r requirements.txt || goto :fail

rem cv2 and numpy are optional extras of PyAutoGUI's image search, which ScrollHarvest never uses.
python -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name ScrollHarvest ^
  --icon scrollharvest_v2.ico ^
  --add-data "scrollharvest_v2.ico;." ^
  --add-data "scrollharvest.ico;." ^
  --add-data "scrollharvest.png;." ^
  --exclude-module cv2 ^
  --exclude-module numpy ^
  scrollharvest.py || goto :fail

echo.
echo Built: %~dp0dist\ScrollHarvest.exe

rem Refresh the installed copy that the Desktop and Start Menu shortcuts open.
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\ScrollHarvest"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /y "dist\ScrollHarvest.exe" "%INSTALL_DIR%\ScrollHarvest.exe" >nul || (
  echo Could not update the installed app - close ScrollHarvest and run this again.
  exit /b 1
)
echo Installed: %INSTALL_DIR%\ScrollHarvest.exe
exit /b 0

:fail
echo.
echo Build failed. Scroll up for the error.
exit /b 1
