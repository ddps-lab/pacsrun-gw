"""HYPERUN-CORS: can the screen actually read this API's answers.

★ WHY THIS FILE EXISTS, AND WHY THE DEFECT IT GUARDS WAS SILENT. The screen lives
on CloudFront and the API on another domain, so every call is cross-origin. Until
2026-09-16 the Lambda Function URL added the CORS headers itself, from its own
`AllowOrigins` configuration, and this application never had to. Moving to an ALB
removed the thing that was doing it -- an ALB forwards what the backend sends and
adds nothing of its own.

Nothing 500s. `app.js` asks for /v1/login-config inside a try/catch, the browser
refuses to hand it the body, the catch returns `{enabled: false}`, and the page
reads that as "this deployment has no Cognito" -- so it offers "Sign in with a
token" and looks like somebody chose that. The server was answering 200 with the
right body the whole time, and it took a person noticing the wrong login screen.

So these tests are about the HEADERS, not about the bodies, which every other
test in this suite already covers.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

UI = "https://screen.example.com"
OTHER = "https://somewhere-else.example.com"


def app_with(origins: str):
    """A bare app carrying exactly the CORS wiring `main` installs on the real one.

    ★ THE REAL APP IS NOT RELOADED HERE, and the reason is not convenience.
    Middleware is added once when a FastAPI app is built and cannot be changed
    afterwards, so a test would have to re-import the module -- which runs the
    lifespan, which wants a cluster. What is worth testing is OUR wiring (which
    origins, which methods, which headers), and `add_cors` is that wiring. The
    line that calls it on the real app is one line below its definition.
    """
    from fastapi import FastAPI
    from ddpsrun_server import main as module

    application = FastAPI()

    @application.get("/v1/login-config")
    def _config():
        return {"enabled": True}

    @application.get("/v1/jobs")
    def _jobs():
        return []

    module.add_cors(application, module.ui_origins({"HYPERUN_UI_ORIGINS": origins}))
    return TestClient(application)


def test_the_screens_origin_is_allowed_to_read_the_answer():
    response = app_with(UI).get("/v1/login-config", headers={"Origin": UI})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == UI, (
        "no allow-origin header: the browser drops the body and the screen falls "
        "back to token sign-in without saying why")


def test_the_preflight_is_answered_for_the_header_the_screen_sends():
    # Every call after login carries `Authorization`, which makes the request
    # non-simple and sends a preflight first. A deployment that answers GET but
    # not OPTIONS works until somebody logs in.
    response = app_with(UI).options("/v1/jobs", headers={
        "Origin": UI,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    assert response.status_code in (200, 204)
    assert "authorization" in response.headers.get("access-control-allow-headers", "").lower()


def test_credentials_are_allowed_because_the_screen_sends_a_token():
    response = app_with(UI).get("/v1/login-config", headers={"Origin": UI})
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_another_origin_is_not_handed_the_answer():
    # ★ NOT `*`. Credentials travel on these requests, and an API that let any
    # page read them would let any page a logged-in person visits act as them.
    response = app_with(UI).get("/v1/login-config", headers={"Origin": OTHER})
    assert response.headers.get("access-control-allow-origin") not in (OTHER, "*")


def test_several_origins_can_be_named():
    # A staging screen and a production one, or a rename in progress.
    client = app_with(f"{UI},{OTHER}")
    for origin in (UI, OTHER):
        response = client.get("/v1/login-config", headers={"Origin": origin})
        assert response.headers.get("access-control-allow-origin") == origin


def test_no_origins_configured_sends_no_headers_and_still_serves():
    # A local run and the test suite are in this state and it is not an error --
    # the API works, it simply cannot be read from a browser on another origin.
    response = app_with("").get("/v1/login-config", headers={"Origin": UI})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_the_old_variable_name_still_names_the_origins():
    from ddpsrun_server import main as module
    assert module.ui_origins({"DDPSRUN_UI_ORIGINS": UI}) == [UI]
    assert module.ui_origins({"HYPERUN_UI_ORIGINS": UI, "DDPSRUN_UI_ORIGINS": OTHER}) == [UI]
