# Docker DDNS Forward Proxy

This service gives you:

- A protected API endpoint to update the current target IP (your home server).
- A public reverse-proxy endpoint that forwards all incoming requests to that current IP.
- Persistent state in a Docker volume, so last known IP survives restarts.

It is designed to run behind Traefik on your VPS.

## How it works

1. Internet user calls your public host, e.g. `ddns.example.com`.
2. Traefik sends the request to this container.
3. This container forwards the request to `http://<current-ip>:<current-port>`.
4. Your home server periodically calls `POST /api/update` to change `<current-ip>`.

## API

### Health check

`GET /healthz`

### Update target IP

`POST /api/update`

Headers:

- `Authorization: Bearer <DDNS_API_TOKEN>` (or `X-API-Token: <token>`)
- `Content-Type: application/json`

Body:

```json
{
  "ip": "203.0.113.42",
  "port": 80,
  "scheme": "http"
}
```

Notes:

- `ip` can be `"auto"` or omitted; then request source IP is used (`X-Forwarded-For` first).
- `scheme` can be `http` or `https`.
- `port` defaults to `DDNS_DEFAULT_UPSTREAM_PORT`.

## Run with Docker Compose (Traefik)

Edit `docker-compose.yml`:

- Set `DDNS_API_TOKEN` to a long random secret.
- Set Traefik host rule to your domain/subdomain.
- Ensure `traefik_proxy` matches your existing external Traefik network.

Start:

```bash
docker compose up -d --build
```

## Example updater call (from home server)

```bash
curl -X POST "https://ddns.example.com/api/update" \
  -H "Authorization: Bearer YOUR_SECRET_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ip":"auto","port":80,"scheme":"http"}'
```

## Security recommendations

- Keep `DDNS_API_TOKEN` secret and long.
- Put `/api/update` behind additional protections if possible (IP allowlist, mTLS, or VPN).
- Prefer HTTPS at Traefik edge.

## Local Docker-only smoke test

Build image:

```bash
docker build -t ddns-proxy:local .
```

Run:

```bash
docker run --rm -p 8080:8080 -e DDNS_API_TOKEN=test-token ddns-proxy:local
```

In another shell:

```bash
curl http://localhost:8080/healthz
```
