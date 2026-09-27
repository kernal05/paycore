FROM python:3.12-slim

WORKDIR /app

COPY services/reconciliation-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
COPY services/reconciliation-service/ /app/services/reconciliation-service/

WORKDIR /app/services/reconciliation-service

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
