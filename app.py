import ipaddress
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def getenv(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def parse_positive_port(value: str, default: int) -> int:
    if not value:
        return default
    parsed = int(value)
    if parsed < 1 or parsed > 65535:
        raise ValueError("port must be between 1 and 65535")
    return parsed


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.target = {
            "ip": "",
            "scheme": getenv("DDNS_DEFAULT_SCHEME", "http").lower() or "http",
            "port": parse_positive_port(getenv("DDNS_DEFAULT_UPSTREAM_PORT", "80"), 80),
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.target["ip"] = data.get("ip", "")
        self.target["scheme"] = data.get("scheme", self.target["scheme"])
        self.target["port"] = int(data.get("port", self.target["port"]))

    def get_target(self) -> dict:
        with self.lock:
            return dict(self.target)

    def set_target(self, ip: str, scheme: str, port: int) -> dict:
        with self.lock:
            self.target["ip"] = ip
            self.target["scheme"] = scheme
            self.target["port"] = port
            self._save_locked()
            return dict(self.target)

    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.target, handle)


class DDNSProxyHandler(BaseHTTPRequestHandler):
    store: StateStore = None
    api_token: str = ""
    proxy_timeout_seconds: int = 10

    def log_message(self, fmt: str, *args) -> None:
        message = "%s - - [%s] %s\n" % (
            self.client_address[0],
            self.log_date_time_string(),
            fmt % args,
        )
        print(message, end="")

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def do_PUT(self) -> None:
        self._route()

    def do_PATCH(self) -> None:
        self._route()

    def do_DELETE(self) -> None:
        self._route()

    def do_HEAD(self) -> None:
        self._route()

    def do_OPTIONS(self) -> None:
        self._route()

    def _route(self) -> None:
        if self.path == "/healthz":
            self._json_response(200, {"ok": True})
            return

        if self.path.startswith("/api/update"):
            self._handle_update()
            return

        if self.path.startswith("/api/target"):
            self._handle_get_target()
            return

        self._forward_request()

    def _authenticate(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        token_header = self.headers.get("X-API-Token", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer ") :].strip()
        else:
            provided = token_header.strip()
        return bool(provided) and provided == self.api_token

    def _handle_update(self) -> None:
        if self.command != "POST":
            self._json_response(405, {"error": "method not allowed"})
            return

        if not self._authenticate():
            self._json_response(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_response(400, {"error": "invalid content length"})
            return

        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response(400, {"error": "invalid JSON"})
            return

        raw_ip = str(payload.get("ip", "")).strip()
        if not raw_ip or raw_ip.lower() == "auto":
            raw_ip = self._ip_from_request()

        try:
            ipaddress.ip_address(raw_ip)
        except ValueError:
            self._json_response(400, {"error": "invalid ip address"})
            return

        scheme = str(payload.get("scheme", "http")).strip().lower() or "http"
        if scheme not in {"http", "https"}:
            self._json_response(400, {"error": "scheme must be http or https"})
            return

        try:
            port = int(payload.get("port", getenv("DDNS_DEFAULT_UPSTREAM_PORT", "80")))
            if port < 1 or port > 65535:
                raise ValueError("bad port")
        except (ValueError, TypeError):
            self._json_response(400, {"error": "invalid port"})
            return

        target = self.store.set_target(raw_ip, scheme, port)
        self._json_response(200, {"updated": True, "target": target})

    def _handle_get_target(self) -> None:
        if self.command != "GET":
            self._json_response(405, {"error": "method not allowed"})
            return

        if not self._authenticate():
            self._json_response(401, {"error": "unauthorized"})
            return

        target = self.store.get_target()
        self._json_response(200, {"target": target})

    def _ip_from_request(self) -> str:
        forwarded_for = self.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            first = forwarded_for.split(",")[0].strip()
            if first:
                return first
        return self.client_address[0]

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _forward_request(self) -> None:
        target = self.store.get_target()
        if not target["ip"]:
            self._json_response(
                503,
                {"error": "no target configured", "hint": "POST /api/update first"},
            )
            return

        host = self._format_host(target["ip"])
        upstream_url = (
            f"{target['scheme']}://{host}:{target['port']}{self.path}"
        )
        upstream_headers = self._filtered_request_headers(host, target["port"])
        upstream_body = self._read_body()
        req = request.Request(
            url=upstream_url,
            data=upstream_body if upstream_body else None,
            headers=upstream_headers,
            method=self.command,
        )

        try:
            with request.urlopen(req, timeout=self.proxy_timeout_seconds) as upstream:
                data = upstream.read()
                self.send_response(upstream.status)
                self._copy_response_headers(upstream.headers)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        except error.HTTPError as http_error:
            data = http_error.read()
            self.send_response(http_error.code)
            self._copy_response_headers(http_error.headers)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (error.URLError, socket.timeout, TimeoutError) as upstream_error:
            self._json_response(
                502,
                {
                    "error": "upstream unavailable",
                    "details": str(upstream_error),
                    "target": target,
                },
            )

    def _format_host(self, ip: str) -> str:
        try:
            parsed = ipaddress.ip_address(ip)
            if parsed.version == 6:
                return f"[{ip}]"
        except ValueError:
            pass
        return ip

    def _filtered_request_headers(self, host: str, port: int) -> dict:
        headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS:
                continue
            if lower == "host":
                continue
            headers[key] = value
        headers["Host"] = f"{host}:{port}"
        return headers

    def _copy_response_headers(self, response_headers) -> None:
        for key, value in response_headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS:
                continue
            if lower == "content-length":
                continue
            self.send_header(key, value)

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def main() -> None:
    listen_host = getenv("DDNS_LISTEN_HOST", "0.0.0.0")
    listen_port = parse_positive_port(getenv("DDNS_LISTEN_PORT", "8080"), 8080)
    api_token = getenv("DDNS_API_TOKEN", "")
    if not api_token:
        raise ValueError("DDNS_API_TOKEN is required")

    state_path = getenv("DDNS_STATE_FILE", "/data/state.json")
    proxy_timeout_seconds = int(getenv("DDNS_PROXY_TIMEOUT_SECONDS", "10"))

    store = StateStore(state_path)
    DDNSProxyHandler.store = store
    DDNSProxyHandler.api_token = api_token
    DDNSProxyHandler.proxy_timeout_seconds = proxy_timeout_seconds

    server = ThreadingHTTPServer((listen_host, listen_port), DDNSProxyHandler)
    print(f"DDNS proxy listening on {listen_host}:{listen_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
