"""A terminal that stays open between two keystrokes. HYPERUN-TERMINAL.

WHAT THIS REPLACES, and why the old shape could not be fixed in place.
`hyperun shell` sends ONE LINE PER HTTPS REQUEST. Each request opens a fresh
WebSocket to the apiserver, types the line, waits for the output to go quiet and
closes. Measured 2026-09-15 against the live gateway with an empty command:
1.12 s, 0.76 s, 1.05 s (median 1.05 s). That is the cost of the connection, not
of the command, and no amount of work on the line protocol removes it.

    The reason it was built that way is written in
    `PACSrun/driver/common/shellsession.py`: "a Lambda execution is capped at 15
    minutes and a training run is thirty hours, so that connection cannot
    exist". The gateway now runs as a POD (config/deploy/hyperun-gw.yaml) behind
    an ALB whose idle timeout is 300 s, and a pod has no such cap -- so the
    connection CAN exist, and this module is it.

END-TO-END FLOW of one keystroke, once `main.py`'s `/v1/jobs/{id}/terminal`
route has accepted the socket:

  1. the browser's xterm.js calls `onData("l")` and sends one BINARY frame
  2. the route puts those bytes on `inbound`, a plain `queue.Queue`
  3. `pump`, on its own thread, takes them off and writes channel 0 of the
     apiserver exec WebSocket -- which is the driver pod's stdin
  4. `shellsession.py attach` in the driver pod forwards them to the shell it
     holds open on the RENTED MACHINE
  5. the remote shell echoes "l" back; it arrives on channel 1
  6. `pump` hands it to `outbound`, which the route has pointed at an asyncio
     queue, and the route sends it to the browser as one BINARY frame

WHY A THREAD AND NOT async ALL THE WAY DOWN. The kubernetes client's exec
channel (`kubernetes.stream.ws_client.WSClient`) is synchronous: it polls a
socket with `select.poll` and blocks. Rewriting that against `aiohttp` would
mean re-implementing the apiserver's auth, its CA handling and its channel
framing here. One thread per open terminal is the smaller thing, and the cost is
bounded by `MAX_TERMINALS` below.

WHY ONE THREAD DOES BOTH DIRECTIONS. `websocket-client`'s `WebSocket` is not
documented to be safe for a send on one thread while another is inside `recv`.
So `pump` interleaves: poll for at most `POLL_SECONDS`, drain whatever arrived,
drain the inbound queue, repeat. The poll IS the pacing; there is no sleep.

THE CHANNEL BYTE IS THE WHOLE PROTOCOL, and it is the same one
`PACSrun/driver/common/remotek8s.py:426` documents for the rented cluster:

    0  stdin    we write this one
    1  stdout   the terminal's output
    2  stderr   only when tty=False; with a tty there is one stream by
               definition and everything arrives on 1
    3  error    a metav1.Status object, and the ONLY place an exit code arrives
    4  resize   `{"Width": cols, "Height": rows}`, added by the v3 subprotocol

★ RESIZE WORKS ON v4 AND AN EARLIER COMMENT IN THIS PROJECT SAID IT DOES NOT.
`PACSrun/driver/common/remotek8s.py:447` reads "v5 adds a close signal and a
resize channel"; the resize channel arrived in v3, not v5.
`k8s.io/apimachinery/pkg/util/remotecommand/constants.go:39-42` says of
`v3.channel.k8s.io`: "adds support for resizing container terminals", and
line 63 of the same file is `StreamResize = 4`. v4 adds exit codes ON TOP of v3,
so the default subprotocol the python client asks for
(`kubernetes/stream/ws_client.py:472`) already carries resize. That is why
`Terminal.resize` below is four lines and needs no protocol negotiation.

Grep anchor: HYPERUN-TERMINAL
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Callable

# The apiserver's exec channels, named rather than spelled 0/1/2/3/4 at each use.
STDIN_CHANNEL = 0
STDOUT_CHANNEL = 1
STDERR_CHANNEL = 2
ERROR_CHANNEL = 3
RESIZE_CHANNEL = 4

# How long one poll waits for bytes from the apiserver before the pump looks at
# the inbound queue instead. It is the keystroke latency floor: a character typed
# the instant after a poll starts waits this long before it is written. 20 ms is
# below what a person can see (the usual figure for "instant" is 100 ms) and
# gives at most 50 wakeups per second per terminal, which is nothing next to the
# thread already existing.
POLL_SECONDS = 0.02

# How many terminals one gateway pod will hold at once. Each costs one OS thread
# and one WebSocket to the apiserver. The deployment runs ONE replica
# (config/deploy/hyperun-gw.yaml:221), so this is the whole cluster's limit, not
# a per-replica one. Eight is a guard against a runaway client rather than a
# capacity plan: this lab has never had eight people debugging eight jobs at
# once, and a refusal that says so is better than a pod that stops answering
# HTTP because every worker thread is in a terminal.
MAX_TERMINALS = 8

# What a browser that opened a socket and then said nothing is given before the
# route drops it. The first message has to be the credential, so this is the
# window for ONE message on an already-established socket.
AUTH_SECONDS = 5.0


class TerminalBusy(Exception):
    """Every terminal slot on this pod is in use."""


class Terminal:
    """One open exec channel, and the thread pumping it.

    The channel is injected rather than opened here so the tests drive the whole
    lifecycle with no cluster -- the same shape
    `PACSrun/driver/common/shellsession.py` uses for `exec_stream`.

    Args:
        channel: anything with the four `WSClient` methods this uses --
            `is_open`, `update`, `read_channel`, `write_channel`, `close`.
        outbound: called with each chunk of terminal output, on the PUMP's
            thread. The route points this at `loop.call_soon_threadsafe`, so
            nothing here ever touches the asyncio loop directly.
        on_close: called once, with the exit code or None, when the channel ends.
    """

    def __init__(self, channel: Any, outbound: Callable[[bytes], None],
                 on_close: Callable[[int | None], None],
                 poll_seconds: float = POLL_SECONDS) -> None:
        self._channel = channel
        self._outbound = outbound
        self._on_close = on_close
        self._poll = poll_seconds
        self._inbound: "queue.Queue[bytes | None]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="hyperun-terminal")
        self._thread.start()

    def close(self) -> None:
        """Ask the pump to stop. Safe to call twice and from any thread."""
        self._stop.set()
        # Wake the pump so it notices without waiting out a poll.
        self._inbound.put(None)

    def join(self, timeout: float = 2.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- what the browser sends ----------------------------------------------

    def write(self, data: bytes) -> None:
        """Keystrokes. Queued rather than written here: see the module docstring
        for why one thread owns the socket."""
        self._inbound.put(data)

    def resize(self, cols: int, rows: int) -> None:
        """Tell the remote PTY how wide the browser's terminal is.

        The payload's field names are `Width` and `Height`, capitalised, because
        the apiserver unmarshals them into Go's `remotecommand.TerminalSize`
        whose fields are exported. Lower-case keys are accepted by the JSON
        decoder and silently leave the size at zero.
        """
        payload = json.dumps({"Width": int(cols), "Height": int(rows)})
        self._inbound.put(_RESIZE_PREFIX + payload.encode("utf-8"))

    # -- the pump ------------------------------------------------------------

    def _pump(self) -> None:
        """Move bytes in both directions until one side stops. Never raises.

        A debugging channel that could raise into the route would close a
        person's terminal on a transient read; every failure here ends the
        session with a reason instead.
        """
        code: int | None = None
        # ★ THE POLL IS ZERO WHILE OUTPUT IS ARRIVING. `WSClient.update` reads AT
        # MOST ONE FRAME per call, so a fixed 20 ms wait would meter a burst at
        # 50 frames a second and `ls -R` would trickle down the screen. Waiting
        # only when the last pass found nothing costs no extra syscall when the
        # terminal is idle, which is almost always.
        wait = self._poll
        try:
            while not self._stop.is_set() and self._channel.is_open():
                # 1. Anything the apiserver has for us. `update` is where the
                #    poll happens, so this is also the loop's only wait.
                self._channel.update(timeout=wait)
                wait = self._poll
                for ch in (STDOUT_CHANNEL, STDERR_CHANNEL):
                    chunk = self._channel.read_channel(ch)
                    if chunk:
                        self._outbound(_as_bytes(chunk))
                        wait = 0.0          # more is probably queued; go get it
                # 2. The error channel carries the exit code and nothing else.
                #    Reading it is how we learn the shell ended, because the
                #    socket can stay open for a moment afterwards.
                status = self._channel.read_channel(ERROR_CHANNEL)
                if status:
                    code = _exit_code(_as_bytes(status))
                    break
                # 3. Whatever the browser typed since the last pass.
                while True:
                    try:
                        item = self._inbound.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:          # the wake-up from `close`
                        continue
                    if item.startswith(_RESIZE_PREFIX):
                        self._channel.write_channel(
                            RESIZE_CHANNEL, item[len(_RESIZE_PREFIX):].decode("utf-8"))
                    else:
                        self._channel.write_channel(STDIN_CHANNEL, item)
        except Exception as exc:              # noqa: BLE001 - see this docstring
            self._outbound(f"\r\n[terminal ended: {exc}]\r\n".encode("utf-8"))
        finally:
            try:
                self._channel.close()
            except Exception:                 # noqa: BLE001 - already ending
                pass
            self._on_close(code)


# An in-band marker on the inbound queue, so one queue carries both keystrokes
# and resizes and the pump keeps owning the socket alone. It is a byte sequence
# no terminal can produce: 0xFF is not valid UTF-8 in any position, and xterm.js
# sends UTF-8.
_RESIZE_PREFIX = b"\xff\xffRESIZE"


def _as_bytes(value: Any) -> bytes:
    """Channel payloads arrive as str unless the client was built binary."""
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def _exit_code(status: bytes) -> int | None:
    """The exit code out of the apiserver's channel-3 `metav1.Status`.

    The object looks like this, and the number is nowhere else -- not in an HTTP
    status, not in a header:

        {"status":"Failure","reason":"NonZeroExitCode",
         "details":{"causes":[{"reason":"ExitCode","message":"1"}]}}

    A success carries `"status":"Success"` and no causes, which is 0.

    Returns:
        The code, or None when the object is not one we recognise. None means
        "we do not know", and the route says so rather than printing 0.
    """
    try:
        obj = json.loads(status.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    if obj.get("status") == "Success":
        return 0
    for cause in ((obj.get("details") or {}).get("causes") or []):
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message"))
            except (TypeError, ValueError):
                return None
    return None


class Slots:
    """How many terminals this pod has open, counted so it can refuse the ninth.

    Not a semaphore: a semaphore blocks, and a route that blocked would hold a
    request worker while the caller waited for somebody else to close a shell.
    This refuses immediately and the browser shows why.
    """

    def __init__(self, limit: int = MAX_TERMINALS) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._open = 0

    def take(self) -> None:
        with self._lock:
            if self._open >= self._limit:
                raise TerminalBusy(
                    f"this gateway pod already has {self._limit} terminals open. "
                    "Close one and try again.")
            self._open += 1

    def give_back(self) -> None:
        with self._lock:
            self._open = max(0, self._open - 1)

    @property
    def open(self) -> int:
        with self._lock:
            return self._open
