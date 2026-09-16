"""The browser terminal: the pump, and the route that holds it open.

WHAT A CLUSTER WOULD ADD AND WHAT IT WOULD NOT. Everything here is about the
wiring -- that a keystroke reaches channel 0, that output comes back as a binary
frame, that a resize is written with the field names the apiserver unmarshals,
that nobody but the owner gets a shell. None of that needs an API server, and a
test that needs one does not run in CI.

The one thing these cannot check is what a real rented machine sends back. The
route starts `PACSrun/driver/common/shell.py`, which has been in the driver image
since 2026-09-07 and is what an operator reaches with `kubectl exec -it`; whether
THAT works is a question for a live job, and it is answered in the raw logs
rather than here.
"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ddpsrun_server import auth, k8s, main, naming, terminal

JOB_ID = "job-a8acdef80a07"
OBJECT_NAME = naming.object_name(JOB_ID)


class FakeChannel:
    """A `WSClient` with the four methods the pump uses, and a script.

    `queued` is what the apiserver "sends": a list of (channel, payload) that
    `update` moves into the read buffers one frame at a time, which is exactly
    how the real client behaves -- `WSClient.update` reads AT MOST ONE FRAME per
    call, and the pump's adaptive poll exists because of it.
    """

    def __init__(self, queued=None):
        self.queued = list(queued or [])
        self.buffers: dict[int, bytes] = {}
        self.written: list[tuple[int, object]] = []
        self.closed = False
        self._open = True
        self.raise_on_update: Exception | None = None

    def is_open(self):
        return self._open

    def update(self, timeout=0):
        if self.raise_on_update is not None:
            raise self.raise_on_update
        if self.queued:
            channel, payload = self.queued.pop(0)
            self.buffers[channel] = self.buffers.get(channel, b"") + payload

    def read_channel(self, channel, timeout=0):
        self.update(timeout=timeout)
        return self.buffers.pop(channel, b"")

    def write_channel(self, channel, data):
        self.written.append((channel, data))

    def close(self):
        self.closed = True
        self._open = False


def drain(condition, seconds=2.0):
    """Wait for a background thread to get somewhere. Returns whether it did."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# ----------------------------------------------------------------- the pump


def test_output_from_the_apiserver_reaches_the_browser():
    channel = FakeChannel([(terminal.STDOUT_CHANNEL, b"hello")])
    seen: list[bytes] = []
    session = terminal.Terminal(channel, outbound=seen.append, on_close=lambda c: None)
    session.start()
    assert drain(lambda: seen == [b"hello"])
    session.close()
    session.join()


def test_stderr_comes_back_too_because_a_non_tty_channel_still_has_one():
    # tty=True merges the two, but `target=driver` with an image whose `sh`
    # refuses a tty falls back to channel 2, and losing it would silently hide
    # every error message.
    channel = FakeChannel([(terminal.STDERR_CHANNEL, b"sh: no such file")])
    seen: list[bytes] = []
    session = terminal.Terminal(channel, outbound=seen.append, on_close=lambda c: None)
    session.start()
    assert drain(lambda: seen == [b"sh: no such file"])
    session.close()
    session.join()


def test_a_keystroke_is_written_to_channel_zero_untouched():
    channel = FakeChannel()
    session = terminal.Terminal(channel, outbound=lambda b: None, on_close=lambda c: None)
    session.start()
    session.write(b"\x03")            # Ctrl+C, which only a PTY can deliver
    assert drain(lambda: (terminal.STDIN_CHANNEL, b"\x03") in channel.written)
    session.close()
    session.join()


def test_resize_uses_the_capitalised_field_names_the_apiserver_unmarshals():
    # ★ LOWER-CASE KEYS ARE ACCEPTED AND SILENTLY DO NOTHING. The apiserver
    # decodes this into Go's `remotecommand.TerminalSize`, whose fields are
    # exported, so `{"width": 120}` leaves the size at zero and a full-screen
    # program draws at 80 columns with no error anywhere.
    channel = FakeChannel()
    session = terminal.Terminal(channel, outbound=lambda b: None, on_close=lambda c: None)
    session.start()
    session.resize(120, 34)
    assert drain(lambda: any(ch == terminal.RESIZE_CHANNEL for ch, _ in channel.written))
    payload = [data for ch, data in channel.written if ch == terminal.RESIZE_CHANNEL][0]
    assert json.loads(payload) == {"Width": 120, "Height": 34}
    session.close()
    session.join()


def test_a_resize_never_reaches_the_shell_as_typed_bytes():
    # The resize rides the same queue as the keystrokes, so the marker that
    # tells them apart has to be something a terminal cannot produce.
    channel = FakeChannel()
    session = terminal.Terminal(channel, outbound=lambda b: None, on_close=lambda c: None)
    session.start()
    session.resize(80, 24)
    session.write(b"ls\n")
    assert drain(lambda: (terminal.STDIN_CHANNEL, b"ls\n") in channel.written)
    typed = [data for ch, data in channel.written if ch == terminal.STDIN_CHANNEL]
    assert typed == [b"ls\n"], "the resize must not have been typed into the shell"
    session.close()
    session.join()


def test_the_exit_code_arrives_on_channel_three_and_ends_the_session():
    status = json.dumps({"status": "Failure", "reason": "NonZeroExitCode",
                         "details": {"causes": [{"reason": "ExitCode",
                                                 "message": "130"}]}}).encode()
    channel = FakeChannel([(terminal.ERROR_CHANNEL, status)])
    ended: list[int | None] = []
    session = terminal.Terminal(channel, outbound=lambda b: None, on_close=ended.append)
    session.start()
    assert drain(lambda: ended == [130])
    assert channel.closed
    session.join()


def test_a_clean_exit_is_zero_and_not_unknown():
    status = json.dumps({"status": "Success"}).encode()
    channel = FakeChannel([(terminal.ERROR_CHANNEL, status)])
    ended: list[int | None] = []
    session = terminal.Terminal(channel, outbound=lambda b: None, on_close=ended.append)
    session.start()
    assert drain(lambda: ended == [0])
    session.join()


def test_an_unreadable_status_is_unknown_rather_than_zero():
    # A zero here would tell a person their command succeeded when nobody knows.
    assert terminal._exit_code(b"not json at all") is None
    assert terminal._exit_code(json.dumps({"status": "Failure"}).encode()) is None


def test_a_channel_that_breaks_ends_the_session_instead_of_raising():
    # ★ A DEBUGGING CHANNEL MUST NEVER BE ABLE TO TAKE ANYTHING ELSE DOWN. The
    # same rule `PACSrun/driver/common/shellsession.py` states for the driver
    # side: every thread is a daemon and every exception is caught.
    channel = FakeChannel()
    channel.raise_on_update = RuntimeError("connection reset")
    seen: list[bytes] = []
    ended: list[int | None] = []
    session = terminal.Terminal(channel, outbound=seen.append, on_close=ended.append)
    session.start()
    assert drain(lambda: ended == [None])
    assert b"connection reset" in b"".join(seen)


def test_close_is_safe_to_call_twice_and_from_any_thread():
    channel = FakeChannel()
    session = terminal.Terminal(channel, outbound=lambda b: None, on_close=lambda c: None)
    session.start()
    threading.Thread(target=session.close).start()
    session.close()
    session.join()
    assert channel.closed


# ----------------------------------------------------------------- the slots


def test_the_ninth_terminal_is_refused_with_a_reason():
    slots = terminal.Slots(limit=2)
    slots.take()
    slots.take()
    with pytest.raises(terminal.TerminalBusy) as caught:
        slots.take()
    assert "2 terminals open" in str(caught.value)
    slots.give_back()
    slots.take()            # the closed one freed a slot
    assert slots.open == 2


# ----------------------------------------------------------------- the route


class FakeCluster:
    """Answers `get_job` from a dict and hands out `FakeChannel`s."""

    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}
        self.opened: list[tuple[str, str, int, list, bool]] = []
        self.channels: list[FakeChannel] = []
        self.refuse: Exception | None = None

    def get_job(self, namespace, name):
        try:
            return self.objects[(namespace, name)]
        except KeyError:
            raise k8s.NotFound(name) from None

    def open_exec_channel(self, namespace, job_name, slot, argv, tty=True):
        if self.refuse is not None:
            raise self.refuse
        self.opened.append((namespace, job_name, slot, argv, tty))
        channel = FakeChannel()
        self.channels.append(channel)
        return channel


def running_job(owner="alice"):
    return {
        "metadata": {"name": OBJECT_NAME,
                     "labels": {"ddpsrun.io/owner": owner}},
        "status": {"phase": "Running"},
    }


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def client(tmp_path, monkeypatch, cluster):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"tokens": [
        {"sha256": auth.hash_token("alice-token"), "user": "alice",
         "namespace": "lab-alice", "team": "lab"},
        {"sha256": auth.hash_token("bob-token"), "user": "bob",
         "namespace": "lab-alice", "team": "lab"},
    ]}))
    monkeypatch.setenv("HYPERUN_RESULT_BUCKET", "<RESULT_BUCKET>")
    monkeypatch.setenv("HYPERUN_TOKENS_PATH", str(tokens))
    monkeypatch.setattr(main.Cluster, "connect", staticmethod(lambda: cluster))
    # A fresh counter per test: it is process-wide on purpose, and a test that
    # left it at 1 would make the next one's limit off by one.
    monkeypatch.setattr(main, "TERMINAL_SLOTS", terminal.Slots())
    with TestClient(main.app) as test_client:
        yield test_client


def open_terminal(client, token="alice-token", query=""):
    return client.websocket_connect(f"/v1/jobs/{JOB_ID}/terminal{query}")


def test_the_first_message_must_carry_a_credential(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"hello": "there"}))
        answer = json.loads(ws.receive_text())
    # ★ NOT "unknown token", WHICH IS WHAT IT USED TO SAY. A message with no
    # token reached the credential check as "" and got an answer that is true and
    # misleading: the reader goes and checks their token, and the fault is in
    # their client. Measured against the live gateway 2026-09-16.
    assert "{\"token\"" in answer["error"], answer
    assert cluster.opened == [], "nothing may be opened before we know who it is"


def test_a_first_message_that_is_not_an_object_says_so_rather_than_vanishing(
        client, cluster):
    # `json.loads("[1,2]")` is a list, and `.get` on a list raises AttributeError.
    # Nothing caught it, so the socket closed with no message and the browser
    # showed a bare close code.
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    with open_terminal(client) as ws:
        ws.send_text("[1, 2]")
        answer = json.loads(ws.receive_text())
    assert "error" in answer
    with open_terminal(client) as ws:
        ws.send_text("not json at all")
        answer = json.loads(ws.receive_text())
    assert "JSON" in answer["error"]


def test_a_credential_nobody_holds_is_refused_in_the_terminal_itself(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "not-a-real-token"}))
        answer = json.loads(ws.receive_text())
    assert "error" in answer
    assert cluster.opened == []


def test_somebody_elses_job_in_the_same_namespace_is_refused(client, cluster):
    # ★ THE NAMESPACE IS NOT THE PERMISSION. alice and bob share `lab-alice`, so
    # a check that stopped at the namespace would give bob alice's shell -- on a
    # machine holding her vendor credentials.
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job(owner="alice")
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "bob-token"}))
        answer = json.loads(ws.receive_text())
    assert "error" in answer
    assert cluster.opened == []


def test_a_finished_job_says_so_rather_than_saying_not_found(client, cluster):
    job = running_job()
    job["status"]["phase"] = "Succeeded"
    cluster.objects[("lab-alice", OBJECT_NAME)] = job
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "alice-token"}))
        answer = json.loads(ws.receive_text())
    assert "Succeeded" in answer["error"]


def test_an_unknown_job_is_refused_before_any_channel_is_opened(client, cluster):
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "alice-token"}))
        answer = json.loads(ws.receive_text())
    assert answer["error"] == "no such job"
    assert cluster.opened == []


def test_the_owner_gets_a_shell_on_the_rented_machine(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "alice-token",
                                 "resize": {"cols": 120, "rows": 34}}))
        ready = json.loads(ws.receive_text())
        assert ready["ready"] is True and ready["target"] == "remote"

        channel = cluster.channels[0]
        # What the browser types goes through untouched.
        ws.send_bytes(b"nvidia-smi\n")
        assert drain(lambda: (terminal.STDIN_CHANNEL, b"nvidia-smi\n") in channel.written)
        # The size went with the first message, so the remote PTY is 120 wide
        # before anything is drawn on it.
        assert any(ch == terminal.RESIZE_CHANNEL for ch, _ in channel.written)
        # And what the machine prints comes back as a binary frame.
        channel.queued.append((terminal.STDOUT_CHANNEL, b"Tesla A100\n"))
        assert ws.receive_bytes() == b"Tesla A100\n"

    namespace, name, slot, argv, tty = cluster.opened[0]
    assert (namespace, name, slot, tty) == ("lab-alice", OBJECT_NAME, 0, True)
    # ★ IT STARTS THE PROGRAM THE DRIVER IMAGE ALREADY HAS. `shell.py` puts its
    # own stdin in raw mode and opens the remote exec with a tty; a job submitted
    # before this route existed therefore works with it.
    assert argv == ["python3", "/app/driver/aws/shell.py", "--slot", "0"]


def test_the_driver_target_opens_a_shell_in_the_driver_pod_itself(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    with open_terminal(client, query="?target=driver") as ws:
        ws.send_text(json.dumps({"token": "alice-token"}))
        assert json.loads(ws.receive_text())["target"] == "driver"
    assert cluster.opened[0][3] == ["sh", "-i"]


def test_a_resize_after_the_window_is_dragged_reaches_the_remote_pty(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "alice-token"}))
        ws.receive_text()
        channel = cluster.channels[0]
        ws.send_text(json.dumps({"resize": {"cols": 200, "rows": 50}}))
        assert drain(lambda: any(ch == terminal.RESIZE_CHANNEL
                                 for ch, _ in channel.written))
    sizes = [json.loads(d) for ch, d in channel.written if ch == terminal.RESIZE_CHANNEL]
    assert {"Width": 200, "Height": 50} in sizes


def test_the_slot_is_given_back_when_the_browser_closes_the_tab(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "alice-token"}))
        ws.receive_text()
        assert main.TERMINAL_SLOTS.open == 1
    assert drain(lambda: main.TERMINAL_SLOTS.open == 0)
    assert cluster.channels[0].closed, "the apiserver channel must not outlive the tab"


def test_a_pod_the_apiserver_will_not_give_us_says_why(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    cluster.refuse = k8s.ClusterError('pods/exec is forbidden')
    with open_terminal(client) as ws:
        ws.send_text(json.dumps({"token": "alice-token"}))
        answer = json.loads(ws.receive_text())
    assert "forbidden" in answer["error"]
    assert main.TERMINAL_SLOTS.open == 0, "a refused open must not leak a slot"


def test_the_ninth_caller_is_told_the_pod_is_full_rather_than_left_hanging(
        client, cluster, monkeypatch):
    cluster.objects[("lab-alice", OBJECT_NAME)] = running_job()
    monkeypatch.setattr(main, "TERMINAL_SLOTS", terminal.Slots(limit=1))
    with open_terminal(client) as first:
        first.send_text(json.dumps({"token": "alice-token"}))
        first.receive_text()
        with open_terminal(client) as second:
            second.send_text(json.dumps({"token": "alice-token"}))
            answer = json.loads(second.receive_text())
    assert "already has 1 terminals open" in answer["error"]
