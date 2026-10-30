@echo off
rem Status board launcher (manual / debug use).
rem Keep this file ASCII: a UTF-8 Chinese comment breaks under cmd's GBK parser.
rem For auto-start without a console window use schtasks + pythonw (README step 5).

setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
python server.py >> "logs\server.log" 2>&1
