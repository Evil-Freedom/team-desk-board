@echo off
rem ============================================================
rem  Switch back to automatic IP (DHCP)
rem  Use this when leaving the office / joining another network
rem  Run as Administrator
rem ============================================================

netsh interface ip set address name="WLAN" dhcp
netsh interface ip set dns name="WLAN" dhcp

echo.
echo   Done. Back to DHCP.
echo.
pause
