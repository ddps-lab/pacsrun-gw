"""Sign in through a browser, the way `aws sso login` does.

END-TO-END FLOW of this file:

  1. `login()` asks the server where its login page is (`GET /v1/login-config`).
     A server with no user pool answers `enabled: false` and the caller falls
     back to `--token`.
  2. It picks a free port from the registered list, and starts a one-request
     HTTP server on it. That server exists only to catch one redirect.
  3. It generates the PKCE pair, opens the browser at Cognito's authorize URL,
     and blocks.
  4. The person signs in. Cognito sends their browser to
     http://localhost:<port>/callback?code=... The server in step 2 takes the
     code, writes a page saying the tab can be closed, and stops.
  5. The code plus the PKCE verifier are exchanged for an id_token and a
     refresh_token, which the caller stores.

WHY A LOCAL HTTP SERVER AT ALL. Cognito never hands the authorization code to a
person; it puts it in a redirect and the browser follows it. In a browser app
that redirect goes to the page's own URL. A CLI has no URL, so it makes one:
`http://localhost:<port>`, which is the only address a browser on the user's own
machine can reach that the CLI also controls (`docs/16-login.md` 16.4).

WHY THE PORT COMES FROM A LIST. Cognito compares `redirect_uri` against its
registered callback URLs character for character. A port nobody registered
cannot receive the code, so the CLI can only use ports the operator put in
`terraform/cognito`'s `callback_urls`.

WHY PKCE. Exchanging a code normally needs a client secret, and a CLI on a
laptop cannot hold one. PKCE replaces it with a random number this process
generates, keeps, and only reveals at the exchange.

Grep anchor: DDPSRUN-CLI-LOGIN
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass

# The ports `terraform/cognito` registers. Keep the two lists identical: a port
# here that is not registered there fails with redirect_mismatch, and a port
# registered there but missing here is simply never used.
CANDIDATE_PORTS = (51234, 51235, 51236, 51237)

HOW_LONG_TO_WAIT_SECONDS = 300


class LoginError(Exception):
    """Signing in did not finish. The CLI prints this and exits non-zero."""


@dataclass(frozen=True)
class Tokens:
    """What a completed sign-in produced.

    Attributes:
        id_token: the JWT sent as `Authorization: Bearer`. Lives an hour.
        refresh_token: buys a new id_token without another browser trip. Lives
            30 days, which is why it is the thing worth storing.
    """

    id_token: str
    refresh_token: str


def _b64(raw: bytes) -> str:
    """base64url with the padding stripped, which is what OAuth asks for."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pkce() -> tuple[str, str]:
    """Make the PKCE pair.

    Returns:
        `(verifier, challenge)`. The verifier never leaves this process until
        the exchange; the challenge is its SHA-256 and is safe to send first.
    """
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _free_port() -> int:
    """The first registered port nothing else is listening on.

    Returns:
        A port number from CANDIDATE_PORTS.

    Raises:
        LoginError: every registered port is busy. Naming them is the useful
            part of the message: the fix is to stop whatever holds one.
    """
    for port in CANDIDATE_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    listed = ", ".join(str(p) for p in CANDIDATE_PORTS)
    raise LoginError(
        f"every port this command may use is busy ({listed}). "
        f"Close whatever is listening on one of them and try again."
    )


DONE_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>ddpsrun</title>
<body style="font-family:system-ui;padding:64px;text-align:center">
<h2>Signed in.</h2><p>You can close this tab and go back to the terminal.</p>
"""

FAILED_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>ddpsrun</title>
<body style="font-family:system-ui;padding:64px;text-align:center">
<h2>Sign-in did not complete.</h2><p>The terminal has the details.</p>
"""


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Answers exactly one GET and records what came with it."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802  (the name is http.server's)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _Catcher.code = (query.get("code") or [None])[0]
        _Catcher.error = (query.get("error_description") or query.get("error") or [None])[0]
        body = DONE_PAGE if _Catcher.code else FAILED_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence. The one line this server would print is noise in a CLI."""


def login_config(server: str, opener=urllib.request.urlopen) -> dict:
    """Ask the server whether Cognito is configured and where its login page is.

    Args:
        server: the gateway URL.
        opener: injected so tests do not need a network.

    Returns:
        The parsed body. `{"enabled": False}` when the server has no user pool,
        and also when it is too old to know this route at all.
    """
    try:
        with opener(f"{server.rstrip('/')}/v1/login-config", timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"enabled": False}
        raise LoginError(f"the server answered {exc.code} when asked how to sign in") from exc
    except Exception as exc:
        raise LoginError(f"could not reach {server}: {exc}") from exc


def exchange(config: dict, form: dict, opener=urllib.request.urlopen) -> Tokens:
    """Trade something for tokens at Cognito's token endpoint.

    Args:
        config: what `login_config` returned.
        form: the grant. Either an authorization_code with its verifier, or a
            refresh_token.
        opener: injected for tests.

    Returns:
        The tokens. On a refresh Cognito returns no new refresh_token, so that
        field comes back empty and the caller keeps the one it already has.

    Raises:
        LoginError: Cognito refused, with its own message where it gave one.
    """
    body = urllib.parse.urlencode({"client_id": config["client_id"], **form}).encode()
    request = urllib.request.Request(
        f"{config['login_domain']}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener(request, timeout=30) as response:
            parsed = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode())
            detail = payload.get("error_description") or payload.get("error") or ""
        except Exception:
            pass
        raise LoginError(f"Cognito refused the exchange: {detail or exc.code}") from exc
    except Exception as exc:
        raise LoginError(f"could not reach Cognito: {exc}") from exc

    return Tokens(
        id_token=parsed.get("id_token", ""),
        refresh_token=parsed.get("refresh_token", ""),
    )


def login(server: str, open_browser=webbrowser.open) -> Tokens:
    """Run the whole browser round trip and return the tokens.

    Args:
        server: the gateway URL.
        open_browser: injected so a test can assert on the URL instead of
            actually opening a browser.

    Returns:
        The tokens.

    Raises:
        LoginError: the server has no Cognito, every port is busy, the person
            closed the browser, or Cognito refused.
    """
    config = login_config(server)
    if not config.get("enabled"):
        raise LoginError(
            "this server does not have browser sign-in configured. "
            "Use `ddpsrun login --server ... --token ...` instead."
        )

    port = _free_port()
    redirect_uri = f"http://localhost:{port}/callback"
    verifier, challenge = _pkce()

    _Catcher.code = None
    _Catcher.error = None
    httpd = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    # One request and then stop. A server that kept listening would be a port
    # left open on the user's machine for as long as the process lived.
    thread = threading.Thread(target=httpd.handle_request, daemon=True)
    thread.start()

    query = urllib.parse.urlencode({
        "client_id": config["client_id"],
        "response_type": "code",
        "scope": " ".join(config.get("scopes", ["openid", "email"])),
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    authorize_url = f"{config['login_domain']}/oauth2/authorize?{query}"

    print("Opening your browser to sign in.")
    print(f"If it did not open, go to:\n  {authorize_url}\n")
    open_browser(authorize_url)

    thread.join(timeout=HOW_LONG_TO_WAIT_SECONDS)
    httpd.server_close()

    if _Catcher.error:
        raise LoginError(f"sign-in was refused: {_Catcher.error}")
    if not _Catcher.code:
        raise LoginError(
            f"nothing came back within {HOW_LONG_TO_WAIT_SECONDS} seconds. "
            f"The browser tab has to finish before this command can."
        )

    return exchange(config, {
        "grant_type": "authorization_code",
        "code": _Catcher.code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    })
