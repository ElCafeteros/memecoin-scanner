@echo off
title Meme Coin Scanner
cd /d "%~dp0"
echo Installing requirements...
pip install -r requirements.txt >nul 2>&1
python memecoin_scanner.py
pause
