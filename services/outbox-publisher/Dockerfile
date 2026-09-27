FROM python:3.12-slim

WORKDIR /app

COPY services/outbox-publisher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
COPY services/outbox-publisher/ /app/services/outbox-publisher/

WORKDIR /app/services/outbox-publisher

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["python", "main.py"]
