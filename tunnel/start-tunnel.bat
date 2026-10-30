@echo off
rem Public tunnel launcher (optional; called by Task Scheduler).
rem The public URL is written to tunnel-url.txt.
rem Keep this file ASCII: a UTF-8 Chinese comment breaks under cmd's GBK parser.

setlocal
cd /d "%~dp0"
node tunnel.js >> "tunnel.log" 2>&1
