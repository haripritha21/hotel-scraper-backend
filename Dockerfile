FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN python -m playwright install chromium
RUN python -m playwright install-deps chromium
COPY . .
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
