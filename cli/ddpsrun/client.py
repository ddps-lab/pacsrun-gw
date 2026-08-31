"""The HTTP calls, and nothing else.

END-TO-END FLOW of this file:

  1. `cli.py` builds a `Client` from the credentials `config.load()` returned.
  2. Every method turns one CLI command into one HTTP request against the
     gateway server's routes.
  3. A non-2xx response becomes a `ServerError` carrying the server's own
     `detail` string, because that string is written for a human — the server
     passes the CRD's validation messages through verbatim for this reason.
  4. `logs()` is a generator so that `--follow` prints lines as they arrive
     rather than buffering a training run's entire output.

WHY THIS IS SEPARATE FROM cli.py. `cli.py` decides what to print; this decides
what to ask for. Keeping them apart means the tests here can use a fake HTTP
layer and the tests there can use a fake client, and neither needs a server.

WHY THERE IS NO RETRY. A submit that timed out may or may not have created a
job, and retrying it would create a second one that also rents a GPU. Until the
API takes an idempotency key, the honest thing is to fail and say so.

Grep anchor: DDPSRUN-CLI-CLIENT
"""

from __future__ import annotations

from typing import Any, Iterator

import requests

# A submit does real work (it creates an object and starts a rental), so it gets
# a longer patience than a status read. Neither is the job's own runtime — the
# server answers immediately in both cases and these only cover the network.
SUBMIT_TIMEOUT = 30
READ_TIMEOUT = 15


class ServerError(Exception):
    """The server refused, or could not be reached. `cli.py` prints and exits 1."""


class Client:
    """One user's connection to one gateway server."""

    def __init__(self, server: str, token: str, session: Any | None = None) -> None:
        """
        Args:
            server: base URL, no trailing slash.
            token: the bearer token.
            session: an object with `.request`, for tests. Defaults to a real
                `requests.Session`.
        """
        self._server = server.rstrip("/")
        self._token = token
        self._session = session or requests.Session()

    def _headers(self, authenticated: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: int = READ_TIMEOUT,
        authenticated: bool = True,
        stream: bool = False,
    ) -> requests.Response:
        """Make one request and turn any failure into a `ServerError`.

        Raises:
            ServerError: the connection failed, timed out, or the response was
                not 2xx. The message is the server's `detail` when there is one.
        """
        try:
            response = self._session.request(
                method,
                f"{self._server}{path}",
                json=json_body,
                headers=self._headers(authenticated),
                timeout=timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise ServerError(f"cannot reach {self._server}: {exc}") from exc

        if response.status_code >= 400:
            raise ServerError(_detail(response))
        return response

    def explain(self) -> str:
        """Fetch the server's own description of itself. No token needed."""
        return self._call("GET", "/v1/explain", authenticated=False).text

    def schema(self) -> dict[str, Any]:
        """Fetch the JSON Schema of a submit request. No token needed."""
        return self._call("GET", "/v1/schema", authenticated=False).json()

    def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        """Ask how long a job will take and what it will cost. Submits nothing."""
        return self._call("POST", "/v1/estimate", json_body=body).json()

    def validate(self, body: dict[str, Any]) -> dict[str, Any]:
        """Ask what is wrong with a job. Submits nothing."""
        return self._call("POST", "/v1/validate", json_body=body).json()

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        """Submit a job.

        Args:
            body: the submit request, already assembled by `cli.py`.

        Returns:
            `{job_id, name, result_path}`.
        """
        return self._call("POST", "/v1/jobs", json_body=body, timeout=SUBMIT_TIMEOUT).json()

    def status(self, job_id: str) -> dict[str, Any]:
        """Read one job's state."""
        return self._call("GET", f"/v1/jobs/{job_id}").json()

    def metrics(self, job_id: str, window_seconds: int = 3600) -> dict[str, Any]:
        """Read a job's GPU usage and training progress."""
        return self._call(
            "GET", f"/v1/jobs/{job_id}/metrics?window_seconds={window_seconds}"
        ).json()

    def logs(self, job_id: str, follow: bool = False) -> Iterator[str]:
        """Yield a job's output line by line.

        Args:
            job_id: the id `submit` returned.
            follow: hold the connection open until the job ends.

        Yields:
            Lines without a trailing newline.

        Raises:
            ServerError: including the "the job has not started a container"
                404, which is the normal answer for the first several minutes
                while a large image is pulled.
        """
        query = "?follow=true" if follow else ""
        # No read timeout while following: the gap between two lines of a
        # training run is minutes, and a timeout would cut the stream every time
        # the model was busy. The connect timeout still applies.
        timeout = (READ_TIMEOUT, None) if follow else READ_TIMEOUT
        response = self._call(
            "GET", f"/v1/jobs/{job_id}/logs{query}", timeout=timeout, stream=True
        )
        for line in response.iter_lines(decode_unicode=True):
            if line is not None:
                yield line


def _detail(response: requests.Response) -> str:
    """Pull the readable part out of an error response.

    FastAPI puts it in `detail`, which is either a string (our own
    `HTTPException`) or a list of field errors (pydantic's 422). Both are worth
    showing; the raw JSON is not.
    """
    try:
        body = response.json()
    except ValueError:
        return f"server returned {response.status_code}"

    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            # loc is like ["body", "gpu", "vram_gb"]; the first element is
            # always "body" and says nothing.
            where = ".".join(str(p) for p in item.get("loc", [])[1:]) or "request"
            parts.append(f"{where}: {item.get('msg', 'invalid')}")
        if parts:
            return "; ".join(parts)
    return f"server returned {response.status_code}"
