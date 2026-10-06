@echo off
cd /d "%~dp0"
py -3 "%~dp0legacy_iow_volume.py" --config "%~dp0pumps.json" %*
exit /b %ERRORLEVEL%
