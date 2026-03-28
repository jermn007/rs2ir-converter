@echo off
echo RS2IR Converter - Build Script
echo ================================

:: Install/upgrade dependencies if needed
pip install --quiet --upgrade pyinstaller mido pillow pycryptodome

:: Clean previous build artifacts
if exist build rmdir /s /q build
if exist "dist\RS2IR Converter" rmdir /s /q "dist\RS2IR Converter"

:: Build
pyinstaller RS2IR_Converter.spec

if errorlevel 1 (
    echo.
    echo BUILD FAILED - see errors above
    pause
    exit /b 1
)

:: Copy license file to dist root so it's visible alongside the exe
copy /y THIRD_PARTY_LICENSES.txt "dist\RS2IR Converter\THIRD_PARTY_LICENSES.txt" >nul

echo.
echo BUILD COMPLETE
echo Distributable folder: dist\RS2IR Converter\
echo Zip that folder and share it.
pause
