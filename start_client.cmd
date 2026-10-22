@echo off
setlocal
set "CODOL_PYTHON=C:\Python314\python.exe"
if not exist "%CODOL_PYTHON%" (
    echo Python not found: %CODOL_PYTHON%
    pause
    exit /b 1
)
set "CFG=%~dp0config\server.ini"
if not exist "%CFG%" (
    echo Config not found: %CFG%
    pause
    exit /b 1
)
set "SERVER_IP="
set "SERVER_PORT="
for /f "usebackq eol=; tokens=1,2 delims==" %%a in ("%CFG%") do (
    if /i "%%a"=="ip" set "SERVER_IP=%%b"
    if /i "%%a"=="port" set "SERVER_PORT=%%b"
)
if not defined SERVER_IP (
    echo server.ini missing [server] ip: %CFG%
    pause
    exit /b 1
)
if not defined SERVER_PORT set "SERVER_PORT=8080"
set "SERVER_IP=%SERVER_IP: =%"
set "SERVER_PORT=%SERVER_PORT: =%"

echo CODOL 1649370 Local Full-Unlock Test
echo Resource validation may take a while. UAC is for the temporary offline guard.
echo Local account: data\accounts. Server: http://%SERVER_IP%:%SERVER_PORT%
"%CODOL_PYTHON%" -B -X utf8 "%~dp0tools\launch_client.py" --resource-library "%~dp0resources\v1649370\offline-image-library" --loose-library "%~dp0resources\v1649370\offline-loose-library" --local-profile --server-url http://%SERVER_IP%:%SERVER_PORT% %*
set "CODOL_EXIT=%ERRORLEVEL%"
if not "%CODOL_EXIT%"=="0" (
    echo Launch or runtime failed. Exit code: %CODOL_EXIT%. Please keep the log.
    pause
)
exit /b %CODOL_EXIT%
