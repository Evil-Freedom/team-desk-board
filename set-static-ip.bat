@echo off
rem ============================================================
rem  Pin this PC to a fixed IP on the office network
rem  Run as Administrator (right-click -> Run as administrator)
rem
rem  EDIT THE FIVE VALUES BELOW before running.
rem ============================================================

set IFACE=WLAN
set IP=192.168.1.100
set MASK=255.255.255.0
set GW=192.168.1.1
set DNS=223.5.5.5

netsh interface ip set address name="%IFACE%" static %IP% %MASK% %GW%
netsh interface ip set dns name="%IFACE%" static %DNS%

echo.
echo   Done. Fixed IP: %IP%
echo   Board:  http://%IP%:8900/
echo.
echo   Leaving the office? Run restore-dhcp.bat
echo.
pause
