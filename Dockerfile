FROM python:3.12-slim

WORKDIR /app
COPY app.py /app/app.py

ENV DDNS_LISTEN_HOST=0.0.0.0 \
    DDNS_LISTEN_PORT=8080 \
    DDNS_STATE_FILE=/data/state.json \
    DDNS_DEFAULT_SCHEME=http \
    DDNS_DEFAULT_UPSTREAM_PORT=80 \
    DDNS_PROXY_TIMEOUT_SECONDS=10

EXPOSE 8080
VOLUME ["/data"]

CMD ["python", "/app/app.py"]
