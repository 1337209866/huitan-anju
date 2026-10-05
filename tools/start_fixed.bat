@echo off
chcp 65001 >nul
title 慧碳安居 - 固定公网通道
cd /d E:\Users\海之子\project\energy-carbon-manager

:: 启动 Flask
start "" /min "D:\anconda\python.exe" "E:\Users\海之子\project\energy-carbon-manager\app.py"
timeout /t 8 /nobreak >nul

:: 启动 localtunnel（固定子域名 huitan-anju，网址永久不变）
start "" /min cmd /c "npx -y localtunnel --port 5000 --subdomain huitan-anju"
exit
