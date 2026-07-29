FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DRY_RUN=true \
    HEALTH_PORT=8080

WORKDIR /app

COPY requirements-cloudrun.txt .
RUN pip install --no-cache-dir -r requirements-cloudrun.txt

COPY . .

EXPOSE 8080

CMD ["python", "scripts/run_live.py", "--dry-run", "--db", "/tmp/bot_state.db"]
