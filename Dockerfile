FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY claim_check/ ./claim_check/

# Fly mounts a persistent volume here for the SQLite ledger.
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "claim_check.app:app", "--host", "0.0.0.0", "--port", "8080"]
