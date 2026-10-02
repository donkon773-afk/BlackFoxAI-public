@echo off
chcp 65001 >nul
title BlackFox AI Workstation - кластер LLM-узлов
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_hub.ps1"
if errorlevel 1 pause
