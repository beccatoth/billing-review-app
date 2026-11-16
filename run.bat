@echo off
REM Double-click on Windows to open the Billing Review app in your browser.
cd /d "%~dp0"
if not exist .venv\Scripts\streamlit.exe (
  echo First run: setting up (about 1 minute)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\pip install -q --upgrade pip
  .venv\Scripts\pip install -q -r requirements.txt
)
echo Starting the Billing Review app...
start "" cmd /c "timeout /t 6 >nul & start http://localhost:8501"
echo Leave this window open while you use the app. Close it to stop the app.
.venv\Scripts\streamlit run app.py --server.address localhost --server.headless true --server.port 8501 --browser.gatherUsageStats false
