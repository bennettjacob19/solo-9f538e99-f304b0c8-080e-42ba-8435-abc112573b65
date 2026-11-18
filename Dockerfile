FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    ZONE_DB=/data/zone.db \
    DNS_BIND=0.0.0.0 \
    DNS_PORT=5353 \
    DNS_QUERY_HOST=127.0.0.1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /data

EXPOSE 8000/tcp 5353/udp
CMD ["python", "-m", "app.main"]
