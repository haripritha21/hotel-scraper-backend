#!/bin/bash
echo "🏨 Starting Hotel Scraper Backend..."
cd "$(dirname "$0")"

# Create virtual environment if not exists
if [ ! -d "venv" ]; then
  echo "📦 Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate

echo "📦 Installing dependencies..."
pip install -r requirements.txt -q

echo "🚀 Starting FastAPI server on http://localhost:8000"
echo "📖 API Docs: http://localhost:8000/docs"
echo ""
uvicorn main:app --reload --port 8000
