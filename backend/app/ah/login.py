"""Interactive OAuth login flow for Albert Heijn.

This is a Python port of the reverse-proxy login flow from
``appie-go/login.go``.  It works by:

1. Starting a local HTTP server on ``127.0.0.1:<random-port>``.
2. Acting as a reverse proxy to ``https://login.ah.nl``.
3. Rewriting ``appie://`` redirect URLs to local callback URLs.
4. Intercepting the callback with the authorization code.
5. The caller exchanges the code for tokens.

If the automatic proxy flow fails, the user can fall back to
manually pasting the redirect URL.
"""

from __future__ import annotations

import gzip
import re
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import structlog

logger = structlog.stdlib.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_LOGIN_BASE_URL = "https://login.ah.nl"
_CLIENT_ID = "appie-ios"


# ---------------------------------------------------------------------------
# Local reverse-proxy login
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a random free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Attributes to strip from Set-Cookie so they work over plain HTTP
_COOKIE_STRIP_RE = re.compile(
    r";\s*(?:secure|samesite\s*=\s*\w+|domain\s*=\s*[^;]+)", re.IGNORECASE
)

_SUCCESS_PAGE = """\
<!DOCTYPE html>
<html>
<head><title>Login Successful</title></head>
<body style="font-family:system-ui;max-width:500px;margin:80px auto;text-align:center">
<h1>Login successful!</h1>
<p>You can close this tab and return to the terminal.</p>
<script>setTimeout(function(){window.close()},1000)</script>
</body>
</html>"""


class _LoginProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler that proxies to login.ah.nl.

    Rewrites responses so the browser-based login flow works through
    our local server instead of requiring the ``appie://`` custom
    scheme.
    """

    # These are set by the factory before the server starts
    local_origin: str = ""
    target_host: str = ""
    code_event: threading.Event
    auth_code: str = ""
    _http_client: httpx.Client

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr logging."""
        pass

    def do_GET(self) -> None:
        self._proxy_request("GET")

    def do_POST(self) -> None:
        self._proxy_request("POST")

    def do_PUT(self) -> None:
        self._proxy_request("PUT")

    def do_OPTIONS(self) -> None:
        self._proxy_request("OPTIONS")

    def _proxy_request(self, method: str) -> None:
        # Intercept callback
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            qs = parse_qs(parsed.query)
            code = qs.get("code", [""])[0]
            logger.info("login_callback_received", code_length=len(code))

            type(self).auth_code = code
            type(self).code_event.set()

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_PAGE.encode())
            return

        # Proxy to login.ah.nl
        target_url = f"https://{self.target_host}{self.path}"

        # Read request body if present
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Forward headers, rewriting origin/referer
        forward_headers = {}
        for key in self.headers:
            if key.lower() in ("host", "accept-encoding"):
                continue
            value = self.headers[key]
            # Rewrite referer/origin to point at the real login server
            if key.lower() in ("referer", "origin"):
                value = value.replace(self.local_origin, f"https://{self.target_host}")
            forward_headers[key] = value

        forward_headers["Host"] = self.target_host

        try:
            resp = self._http_client.request(
                method,
                target_url,
                headers=forward_headers,
                content=body,
                follow_redirects=False,
            )
        except Exception as exc:
            logger.error("login_proxy_error", error=str(exc))
            self.send_error(502, f"Proxy error: {exc}")
            return

        # Rewrite the response
        self._send_proxied_response(resp)

    def _send_proxied_response(self, resp: httpx.Response) -> None:
        """Send the proxied response back to the browser, rewriting as needed."""
        self.send_response(resp.status_code)

        # Process response headers
        body_is_text = False
        for key, value in resp.headers.multi_items():
            lower_key = key.lower()

            # Skip hop-by-hop and problematic headers
            if lower_key in (
                "transfer-encoding", "connection", "keep-alive",
                "content-security-policy", "strict-transport-security",
                "x-frame-options", "content-encoding", "content-length",
            ):
                continue

            # Rewrite Location header
            if lower_key == "location":
                if value.startswith("appie://"):
                    # This is the auth code redirect — rewrite to our callback
                    parsed_loc = urlparse(value)
                    qs = parsed_loc.query
                    value = f"{self.local_origin}/callback?{qs}"
                    logger.info("login_redirect_intercepted")
                elif f"https://{self.target_host}" in value:
                    value = value.replace(
                        f"https://{self.target_host}", self.local_origin
                    )
                self.send_header(key, value)
                continue

            # Sanitize Set-Cookie
            if lower_key == "set-cookie":
                value = _COOKIE_STRIP_RE.sub("", value)
                self.send_header(key, value)
                continue

            # Track if body is text
            if lower_key == "content-type":
                body_is_text = any(
                    t in value
                    for t in ("text/html", "javascript", "json")
                )

            self.send_header(key, value)

        # Read and potentially rewrite body
        raw_body = resp.content

        # Decompress if gzipped
        if resp.headers.get("content-encoding") == "gzip":
            try:
                raw_body = gzip.decompress(raw_body)
            except Exception:
                pass

        if body_is_text and raw_body:
            text = raw_body.decode("utf-8", errors="replace")
            text = text.replace("appie://login-exit", f"{self.local_origin}/callback")
            text = text.replace(
                f"https://{self.target_host}", self.local_origin
            )
            raw_body = text.encode("utf-8")

        self.send_header("Content-Length", str(len(raw_body)))
        self.end_headers()
        self.wfile.write(raw_body)


def interactive_login(
    *,
    client_id: str = _CLIENT_ID,
    login_base_url: str = _LOGIN_BASE_URL,
    timeout: float = 300.0,
) -> str:
    """Run the interactive browser login and return the authorization code.

    Opens the user's browser to the AH login page (proxied through a
    local server).  Returns the authorization code after the user
    successfully logs in.

    Args:
        client_id: The OAuth client ID.
        login_base_url: Base URL for the login server.
        timeout: How long to wait for login (seconds).

    Returns:
        The authorization code string.

    Raises:
        TimeoutError: If the user doesn't complete login within timeout.
        RuntimeError: If the login flow fails.
    """
    port = _find_free_port()
    local_origin = f"http://127.0.0.1:{port}"
    target = urlparse(login_base_url)
    target_host = target.netloc or target.hostname or "login.ah.nl"

    # Set up the handler class with shared state
    code_event = threading.Event()
    http_client = httpx.Client(timeout=30.0, follow_redirects=False)

    _LoginProxyHandler.local_origin = local_origin
    _LoginProxyHandler.target_host = target_host
    _LoginProxyHandler.code_event = code_event
    _LoginProxyHandler.auth_code = ""
    _LoginProxyHandler._http_client = http_client

    server = HTTPServer(("127.0.0.1", port), _LoginProxyHandler)

    # Run server in background thread
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    login_url = (
        f"{local_origin}/login"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&redirect_uri=appie://login-exit"
    )

    print(f"\nOpening Albert Heijn login page in your browser...\n")
    print(f"If the browser doesn't open, visit:")
    print(f"  {login_url}\n")
    print(f"Waiting for login...\n")

    try:
        webbrowser.open(login_url)
    except Exception:
        pass  # URL was already printed above

    try:
        # Wait for the callback
        if not code_event.wait(timeout=timeout):
            raise TimeoutError(
                f"Login timed out after {timeout:.0f}s. "
                f"Please try again."
            )

        code = _LoginProxyHandler.auth_code
        if not code:
            raise RuntimeError("Login callback received but no code was present.")

        return code

    finally:
        server.shutdown()
        http_client.close()


def extract_code_from_url(url: str) -> str:
    """Extract the authorization code from an ``appie://`` redirect URL.

    This is the manual fallback: the user copies the URL from the
    browser's address bar after the redirect fails.

    Args:
        url: The full redirect URL (``appie://login-exit?code=...``)
             or just the authorization code itself.

    Returns:
        The authorization code.

    Raises:
        ValueError: If no code could be extracted.
    """
    # If it looks like a bare code (no URL structure), return it
    if "://" not in url and "?" not in url and len(url) > 10:
        return url.strip()

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    codes = qs.get("code", [])
    if codes:
        return codes[0]

    raise ValueError(
        f"Could not extract authorization code from URL: {url!r}"
    )
