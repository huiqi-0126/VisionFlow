@echo off
echo Starting cloneVideo Web Server...
cd cloneVideo
echo Installing dependencies...
pip install -r requirements.txt
python main.py -v web
pause
