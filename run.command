#!/bin/bash
# Double-click on a Mac to open the Billing Review app in your browser.
cd "$(dirname "$0")"
PORT=8501
URL="http://localhost:$PORT"

# Already running? Just open it.
if curl -s "$URL/_stcore/health" >/dev/null 2>&1; then
  open "$URL"; exit 0
fi

if [ ! -x .venv/bin/streamlit ]; then
  echo "First run: setting up (about 1 minute)…"
  python3 -m venv .venv || { echo "Python 3 is required: https://www.python.org/downloads/"; read -r; exit 1; }
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

echo "Starting the Billing Review app…"
# headless = no Streamlit email prompt; we open the browser ourselves once the app is ready
PYTHONWARNINGS=ignore .venv/bin/streamlit run app.py --server.address localhost --server.headless true --server.port $PORT --browser.gatherUsageStats false &
APP_PID=$!
for i in $(seq 1 60); do
  curl -s "$URL/_stcore/health" >/dev/null 2>&1 && break
  sleep 1
done
open "$URL"
echo ""
echo "✅ The app is open in your browser at $URL"
echo "   Leave this window open while you use it. Close it (or press Ctrl+C) to stop the app."
wait $APP_PID
