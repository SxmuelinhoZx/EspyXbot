#!/bin/bash
cd "$(dirname "$0")"
python3 -c "import requests, websocket" 2>/dev/null || python3 -m pip install requests websocket-client==1.8.0
python3 app.py
