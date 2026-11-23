@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONPATH=%CD%\src"
set "BUILD_PYTHON=..\verification\client\workspaces\host-build-env\Scripts\python.exe"
if exist "%BUILD_PYTHON%" goto compile
set "BUILD_PYTHON=workspaces\nuitka-env\Scripts\python.exe"
if not exist "%BUILD_PYTHON%" (
    py -3.14 -m venv workspaces\nuitka-env || goto failed
)
"%BUILD_PYTHON%" -m pip install -r requirements-build.txt || goto failed

:compile
"%BUILD_PYTHON%" -m nuitka --mode=onefile --enable-plugin=tk-inter --msvc=latest --assume-yes-for-downloads --windows-console-mode=disable --windows-icon-from-exe=bin/v1649370/codol1649370-offline.exe --output-filename=CODOLLauncher.exe --output-dir=workspaces/launcher-build tools/launch_updater.py || goto failed
if not exist "..\outputs\" mkdir "..\outputs" || goto failed
copy /y "workspaces\launcher-build\CODOLLauncher.exe" "..\outputs\CODOLLauncher.exe" >nul || goto failed
echo Built: "%CD%\..\outputs\CODOLLauncher.exe"
popd
exit /b 0

:failed
echo Build failed. If the output EXE is in use, close the launcher and retry.
pause
popd
exit /b 1
