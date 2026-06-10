FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN playwright install-deps chromium
RUN playwright install chromium

# ✅ Move Chrome to the correct location
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright

RUN playwright install chromium

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
