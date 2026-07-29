@echo off
cd /d "%~dp0"
start "" http://127.0.0.1:8770/
"C:/Users/admin/.workbuddy/binaries/python/versions/3.13.12/python.exe" server.py
