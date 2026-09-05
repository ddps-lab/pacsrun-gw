"""Does the browser sign-in build the right URL, and refuse the wrong things?

No browser and no network. `open_browser` and the HTTP openers are injected, so
what is exercised is the URL this code builds, the PKCE pair it derives, and
what it does when something goes wrong.

The PKCE check is the one that matters: if the challenge is not really the
SHA-256 of the verifier, Cognito rejects the exchange and the failure surfaces
only against a live user pool.

Grep anchor: DDPSRUN-CLI-LOGIN-TESTS
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
import urllib.parse

import pytest

from ddpsrun import browser_login

# Shaped like what `GET /v1/login-config` returns, with made-up identifiers.
# The real pool id and client id stay out of the repo for the same reason the
# account number does: this repository is meant to be opened later, and an
# identifier committed now is an identifier somebody has to find and remove then
# (`.github/workflows/ci.yml` header).
CONFIG = {
    "enabled": True,
    "client_id": "examplecl1entid0000000000",
    "issuer": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Example00",
    "login_domain": "https://example-pool.auth.us-west-2.amazoncognito.com",
    "scopes": ["openid", "email"],
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(payload):
    return lambda *a, **k: FakeResponse(json.dumps(payload).encode())


# ------------------------------------------------------------------- PKCE


def test_the_challenge_really_is_the_sha256_of_the_verifier():
    """Derived the same way Cognito derives it, and compared."""
    verifier, challenge = browser_login._pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected


def test_the_verifier_is_long_enough_and_unpadded():
    """RFC 7636 wants 43 to 128 characters, and no base64 padding anywhere."""
    verifier, challenge = browser_login._pkce()
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "=" not in challenge


def test_two_sign_ins_never_share_a_verifier():
    assert browser_login._pkce()[0] != browser_login._pkce()[0]


# ----------------------------------------------------------- login-config


def test_a_server_with_no_user_pool_says_so_rather_than_failing():
    config = browser_login.login_config("https://x", opener=opener_returning({"enabled": False}))
    assert config == {"enabled": False}


def test_a_server_too_old_to_know_the_route_is_treated_as_no_pool():
    """A 404 means this build predates Cognito, not that anything is broken."""

    def opener(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    assert browser_login.login_config("https://x", opener=opener) == {"enabled": False}


def test_an_unreachable_server_is_a_LoginError_naming_the_address():
    def opener(*a, **k):
        raise OSError("connection refused")

    with pytest.raises(browser_login.LoginError, match="https://x"):
        browser_login.login_config("https://x", opener=opener)


# ------------------------------------------------------- the authorize URL


def test_the_browser_is_sent_to_cognito_with_everything_it_needs(monkeypatch):
    opened = {}

    monkeypatch.setattr(browser_login, "login_config", lambda server: CONFIG)
    monkeypatch.setattr(browser_login, "_free_port", lambda: 51234)
    monkeypatch.setattr(browser_login, "exchange",
                        lambda cfg, form, **k: browser_login.Tokens("id.tok.en", "refresh"))

    class InstantServer:
        def __init__(self, *a, **k):
            pass

        def handle_request(self):
            browser_login._Catcher.code = "the-code"

        def server_close(self):
            pass

    monkeypatch.setattr(browser_login.http.server, "HTTPServer", InstantServer)

    def fake_open(url):
        opened["url"] = url
        return True

    browser_login.login("https://gw.example", open_browser=fake_open)

    parts = urllib.parse.urlsplit(opened["url"])
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert parts.netloc == "example-pool.auth.us-west-2.amazoncognito.com"
    assert parts.path == "/oauth2/authorize"
    assert query["client_id"] == CONFIG["client_id"]
    assert query["response_type"] == "code"
    assert query["scope"] == "openid email"
    assert query["code_challenge_method"] == "S256"
    # The port has to be one Cognito was told about, or it answers
    # redirect_mismatch (verified against the live pool on 2026-09-02).
    assert query["redirect_uri"] == "http://localhost:51234/callback"


def test_the_verifier_itself_never_goes_out_in_the_url(monkeypatch):
    """The whole point of PKCE. Only its hash goes out at this stage."""
    seen = {}
    made = {}

    real_pkce = browser_login._pkce

    def spy():
        verifier, challenge = real_pkce()
        made["verifier"] = verifier
        return verifier, challenge

    monkeypatch.setattr(browser_login, "_pkce", spy)
    monkeypatch.setattr(browser_login, "login_config", lambda server: CONFIG)
    monkeypatch.setattr(browser_login, "_free_port", lambda: 51234)
    monkeypatch.setattr(browser_login, "exchange",
                        lambda cfg, form, **k: browser_login.Tokens("i", "r"))

    class InstantServer:
        def __init__(self, *a, **k):
            pass

        def handle_request(self):
            browser_login._Catcher.code = "c"

        def server_close(self):
            pass

    monkeypatch.setattr(browser_login.http.server, "HTTPServer", InstantServer)
    browser_login.login("https://gw", open_browser=lambda url: seen.update(url=url))
    assert made["verifier"] not in seen["url"]


def test_the_verifier_IS_sent_at_the_exchange(monkeypatch):
    """And it must be, or Cognito cannot check it against the challenge."""
    sent = {}
    monkeypatch.setattr(browser_login, "login_config", lambda server: CONFIG)
    monkeypatch.setattr(browser_login, "_free_port", lambda: 51234)
    monkeypatch.setattr(browser_login, "exchange",
                        lambda cfg, form, **k: sent.update(form) or browser_login.Tokens("i", "r"))

    class InstantServer:
        def __init__(self, *a, **k):
            pass

        def handle_request(self):
            browser_login._Catcher.code = "the-code"

        def server_close(self):
            pass

    monkeypatch.setattr(browser_login.http.server, "HTTPServer", InstantServer)
    browser_login.login("https://gw", open_browser=lambda url: None)
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert sent["code_verifier"]


# ------------------------------------------------------------- 실패할 때


def test_a_server_without_cognito_says_to_use_a_token(monkeypatch):
    monkeypatch.setattr(browser_login, "login_config", lambda server: {"enabled": False})
    with pytest.raises(browser_login.LoginError, match="--token"):
        browser_login.login("https://gw", open_browser=lambda url: None)


def test_cognito_refusing_the_exchange_carries_its_own_message():
    def opener(*a, **k):
        raise urllib.error.HTTPError(
            "u", 400, "Bad Request", {},
            io.BytesIO(json.dumps({"error_description": "PKCE verification failed"}).encode()),
        )

    with pytest.raises(browser_login.LoginError, match="PKCE verification failed"):
        browser_login.exchange(CONFIG, {"grant_type": "refresh_token"}, opener=opener)


def test_every_port_busy_names_the_ports(monkeypatch):
    import socket as socket_module

    class Busy:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def bind(self, *a):
            raise OSError("in use")

    monkeypatch.setattr(socket_module, "socket", lambda *a, **k: Busy())
    with pytest.raises(browser_login.LoginError, match="51234"):
        browser_login._free_port()


def test_a_refresh_keeps_the_refresh_token_we_already_have():
    """Cognito returns no new refresh_token on a refresh, so an empty one here
    must not overwrite the stored one."""
    tokens = browser_login.exchange(
        CONFIG, {"grant_type": "refresh_token"},
        opener=opener_returning({"id_token": "new.id.token"}),
    )
    assert tokens.id_token == "new.id.token"
    assert tokens.refresh_token == ""


# ------------------------------------------------------- 조용한 자동 갱신


def a_token_expiring_in(seconds: int) -> str:
    """Build a JWT-shaped string with an exp. Not signed: `refreshed` only reads
    the claim to decide when to renew, and the server is what verifies."""
    import time

    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + seconds}).encode()
    ).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def test_a_token_with_time_left_is_not_touched(monkeypatch, tmp_path):
    from ddpsrun import cli, config

    called = {"n": 0}
    monkeypatch.setattr(cli.browser_login, "login_config",
                        lambda s: called.update(n=called["n"] + 1) or CONFIG)
    creds = config.Credentials("https://gw", a_token_expiring_in(3600), "refresh-tok")
    assert cli.refreshed(creds) is creds
    assert called["n"] == 0, "it asked Cognito for nothing, which is the point"


def test_an_expired_token_is_renewed_silently(monkeypatch, tmp_path):
    from ddpsrun import cli, config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cli.browser_login, "login_config", lambda s: CONFIG)
    monkeypatch.setattr(cli.browser_login, "exchange",
                        lambda cfg, form, **k: browser_login.Tokens("fresh.id.token", ""))

    creds = config.Credentials("https://gw", a_token_expiring_in(-10), "refresh-tok")
    renewed = cli.refreshed(creds)
    assert renewed.token == "fresh.id.token"
    # Cognito mints no new refresh token on a refresh, so the old one survives.
    assert renewed.refresh_token == "refresh-tok"
    # And it was written back, or the next command would refresh all over again.
    assert json.loads(config.config_path().read_text())["token"] == "fresh.id.token"


def test_a_static_token_is_never_refreshed(monkeypatch):
    """It has no exp and no refresh token. This must not crash on either."""
    from ddpsrun import cli, config

    creds = config.Credentials("https://gw", "ddpsrun-static-abc", "")
    assert cli.refreshed(creds) is creds


def test_a_failed_renewal_returns_the_old_token_rather_than_raising(monkeypatch):
    """The request then fails on its own with the server's own 401, which says
    more than an error raised from in here would."""
    from ddpsrun import cli, config

    def boom(*a, **k):
        raise browser_login.LoginError("Cognito is down")

    monkeypatch.setattr(cli.browser_login, "login_config", boom)
    creds = config.Credentials("https://gw", a_token_expiring_in(-10), "refresh-tok")
    assert cli.refreshed(creds) is creds
