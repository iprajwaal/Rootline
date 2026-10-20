FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.12 "uvicorn[standard]==0.34.2" \
    "psycopg[binary,pool]==3.2.9" redis==5.2.1 confluent-kafka==2.10.0 httpx==0.28.1
COPY app.py worker.py traffic.py alerter.py ./
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
