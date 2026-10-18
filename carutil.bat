@echo off
setlocal
cd /d "%~dp0"

echo.
echo CarUtil setup
echo.

set "PYTHON="
set "INSTALL_PYTHON="
set "INSTALL_PACKAGES="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON=python"
)

if defined PYTHON goto :python_found
echo Python was not found. CarUtil needs Python 3.9 or newer.
echo.
set /p "INSTALL_PYTHON=Install Python with winget now? [y/N] "
if /i not "%INSTALL_PYTHON%"=="y" goto :python_help
where winget >nul 2>nul
if errorlevel 1 goto :python_help
winget install --id Python.Python.3.14 --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :setup_failed
echo.
echo Python was installed. Close this window, then double-click
echo carutil.bat again so Windows can refresh its PATH.
goto :done

:python_found
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 (
    echo CarUtil needs Python 3.9 or newer. Update Python, then run this file again.
    goto :done
)

call :check_imagemagick

if not exist ".venv\Scripts\python.exe" (
    echo Creating CarUtil's private Python environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :setup_failed
)

".venv\Scripts\python.exe" -c "import questionary, rich" >nul 2>nul
if not errorlevel 1 goto :start_carutil
echo CarUtil's enhanced menu packages are not installed.
set /p "INSTALL_PACKAGES=Install them now? [Y/n] "
if /i "%INSTALL_PACKAGES%"=="n" goto :start_carutil
if /i "%INSTALL_PACKAGES%"=="no" goto :start_carutil
echo Installing CarUtil's required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :setup_failed

:start_carutil
echo.
echo Starting CarUtil...
echo.
".venv\Scripts\python.exe" carutil.py
goto :done

:python_help
echo.
echo Install Python 3.9 or newer from https://www.python.org/downloads/
echo Then close this window and double-click carutil.bat again.
goto :done

:setup_failed
echo.
echo Setup did not finish. Check the message above, fix the issue, and run
echo carutil.bat again. Your car conversion has not started yet.
goto :done

:check_imagemagick
call :verify_imagemagick
if not errorlevel 1 exit /b 0
echo ImageMagick is optional; it converts the bundled brand logos.
:imagemagick_prompt
set "INSTALL_MAGICK=y"
set /p "INSTALL_MAGICK=Install ImageMagick now? [Y/n] "
if /i "%INSTALL_MAGICK%"=="n" goto :imagemagick_skipped
if /i "%INSTALL_MAGICK%"=="no" goto :imagemagick_skipped
if /i "%INSTALL_MAGICK%"=="y" goto :imagemagick_install
if /i "%INSTALL_MAGICK%"=="yes" goto :imagemagick_install
echo Please answer y or n.
goto :imagemagick_prompt

:imagemagick_install
where winget >nul 2>nul
if errorlevel 1 (
    echo winget was not found. Install ImageMagick from https://imagemagick.org/
    echo CarUtil will continue without replacing the brand logo.
    exit /b 0
)
winget install --id ImageMagick.ImageMagick --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo ImageMagick could not be installed. CarUtil will continue without replacing the brand logo.
    exit /b 0
)
for /d %%D in ("%ProgramFiles%\ImageMagick*") do if exist "%%~D\magick.exe" set "PATH=%%~D;%PATH%"
for /d %%D in ("%ProgramFiles(x86)%\ImageMagick*") do if exist "%%~D\magick.exe" set "PATH=%%~D;%PATH%"
call :verify_imagemagick
if errorlevel 1 (
    echo ImageMagick was installed but is not available in this session.
    echo Restart carutil.bat if you want to use the bundled brand logos.
    exit /b 0
)
echo ImageMagick is ready.
exit /b 0

:imagemagick_skipped
echo Continuing without ImageMagick; the mod's logo or a placeholder will be used.
exit /b 0

:verify_imagemagick
set "MAGICK_FOUND="
magick -version 2>&1 | findstr /i "ImageMagick" >nul
if not errorlevel 1 set "MAGICK_FOUND=1"
if defined MAGICK_FOUND exit /b 0
convert -version 2>&1 | findstr /i "ImageMagick" >nul
if not errorlevel 1 set "MAGICK_FOUND=1"
if defined MAGICK_FOUND exit /b 0
exit /b 1

:done
echo.
pause
