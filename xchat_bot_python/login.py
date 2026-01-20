from __future__ import annotations

import os
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler

from xdk import Client

from .env import load_env
from .state import load_state, save_state


def main() -> None:
    env = load_env()
    client_id = env.get("OAUTH_CLIENT_ID")
    redirect_uri = env.get("OAUTH_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise SystemExit("Missing OAUTH_CLIENT_ID or OAUTH_REDIRECT_URI in .env")

    redirect = urllib.parse.urlparse(redirect_uri)
    if redirect.hostname not in ("localhost", "127.0.0.1"):
        raise SystemExit("OAUTH_REDIRECT_URI must use localhost for auto login")
    host = redirect.hostname or "localhost"
    port = redirect.port or 8080
    path = redirect.path or "/callback"

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    scope = env.get("OAUTH_SCOPES") or "dm.read dm.write tweet.read users.read"
    client = Client(
        client_id=client_id,
        client_secret=env.get("OAUTH_CLIENT_SECRET"),
        redirect_uri=redirect_uri,
        scope=scope,
        base_url=env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
    )

    callback_holder: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return
            if parsed.query:
                callback_holder["url"] = (
                    f"http://{host}:{port}{self.path}"
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Login complete. You can close this tab.")
                return
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing code parameter.")

        def log_message(self, format: str, *args) -> None:
            return

    class Server(socketserver.TCPServer):
        allow_reuse_address = True

    server = Server((host, port), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = client.get_authorization_url()
    print("Opening browser for OAuth login...")
    webbrowser.open(url)

    start = time.time()
    while thread.is_alive() and time.time() - start < 300:
        time.sleep(0.1)
    server.server_close()

    if "url" not in callback_holder:
        raise SystemExit("Timed out waiting for OAuth callback")
    token = client.fetch_token(callback_holder["url"])
    state = load_state()
    state["oauth_token"] = token
    save_state(state)
    print("Saved OAuth token to state.json")


if __name__ == "__main__":
    main()

