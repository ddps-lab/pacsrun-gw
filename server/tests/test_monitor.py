"""HYPERUN-MONITOR: does a rule fire on the run it was built for, and stay quiet otherwise.

THE TWO FAILURES THESE TESTS DEFEND AGAINST, in the order they matter:

  * missing a real one. The run that prompted this work finished `Succeeded`
    after 9 h 44 m at $6.36/hour with one of its nine trainings 25% downhill.
    `test_the_real_run_that_went_backwards_is_caught` is that run's own numbers.
  * crying wolf. A monitor that fires on a healthy job gets muted, and a muted
    monitor is worth less than none. Most of the cases below are the quiet ones.

WHAT IS DELIBERATELY NOT TESTED HERE: whether the AI writes a good sentence.
It is not asked to decide anything, so there is nothing to assert about its
judgement -- and `explain` returning "" is a supported answer, which
`test_a_model_that_cannot_be_reached_still_sends_the_numbers` pins down.
"""
from __future__ import annotations

import json
import time

import pytest

from ddpsrun_server import metrics, monitor


# The real series. Scores from `ppo_stats.jsonl`, `bank/adapters/AD/iter_1`,
# read out of the results bucket on 2026-09-15.
REAL_SCORES = [2.4413, 2.7344, 3.6300, 3.0712, 2.3713, 2.4700, 2.2800, 3.1000,
               2.4000, 3.3800, 2.9400, 3.4100, 3.0800, 2.8100, 3.4400, 1.5100,
               3.0000, 1.5400, 2.2800, 2.3700]

NOW = 1_800_000_000.0


def lines_for(values, field="score", series="bank/adapters/AD/iter_1", start=NOW - 600):
    """Log lines exactly as metric-watch.sh prints them, one per step."""
    out = []
    for i, value in enumerate(values, start=1):
        row = {"_series": series, "step": i, field: value}
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start + i)) + ".000000000Z"
        out.append(stamp + " PACSRUN_METRIC=" + json.dumps(row, separators=(",", ":")))
    return out


def findings(lines, now=NOW):
    reading = metrics.scan(lines, monitor.WINDOW_SECONDS)
    return monitor.findings_for(reading, lines, now, monitor._last_line_time(lines))


def rules(lines, now=NOW):
    return sorted(f["rule"] for f in findings(lines, now))


# ---------------------------------------------------------------- catching a real one


def test_the_real_run_that_went_backwards_is_caught():
    # ★ THE CASE THE WHOLE FEATURE EXISTS FOR. This job reported `Succeeded` with
    # a perfect progress bar. Its objective fell 25% and every log it uploaded
    # contained the word `loss` zero times.
    found = findings(lines_for(REAL_SCORES))
    regressions = [f for f in found if f["rule"] == "regression"]
    assert len(regressions) == 1
    hit = regressions[0]
    assert hit["field"] == "score"
    assert hit["series"] == "bank/adapters/AD/iter_1"
    assert hit["change_ratio"] == pytest.approx(-0.249, abs=0.002)
    assert "went the wrong way" in hit["detail"]


def test_a_loss_that_rises_is_caught_and_a_loss_that_falls_is_not():
    # The direction comes from the field's NAME, which is the only thing the
    # server can know. `loss` going up and `score` going down are the same event.
    rising = [1.0 + 0.1 * i for i in range(20)]
    assert "regression" in rules(lines_for(rising, field="train_loss"))
    assert "regression" not in rules(lines_for(list(reversed(rising)), field="train_loss"))


def test_a_field_whose_name_says_nothing_is_never_called_a_regression():
    # `kl` halving or doubling is meaningful to the researcher and meaningless to
    # us. Guessing a direction here would produce confident nonsense, which is
    # the one thing worse than saying nothing.
    assert "regression" not in rules(lines_for([0.1 * i for i in range(1, 21)], field="kl"))
    assert "regression" not in rules(
        lines_for(list(reversed([0.1 * i for i in range(1, 21)])), field="kl"))


def test_a_nan_anywhere_in_a_series_is_reported():
    values = REAL_SCORES[:9] + [float("nan")] + REAL_SCORES[10:]
    found = [f for f in findings(lines_for(values)) if f["rule"] == "nan"]
    assert len(found) == 1
    assert found[0]["field"] == "score"


@pytest.mark.parametrize("marker", [
    "Traceback (most recent call last):",
    "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
    "NCCL error in: ../torch/csrc/distributed/c10d/ProcessGroupNCCL.cpp",
])
def test_a_crash_marker_in_the_log_is_reported(marker):
    lines = lines_for(REAL_SCORES[:5]) + [f"2026-09-15T00:00:00.000000000Z {marker}"]
    assert "crash" in rules(lines, now=NOW)


def test_silence_is_measured_from_the_newest_line_and_not_from_the_window():
    quiet = lines_for(REAL_SCORES[:5], start=NOW - monitor.SILENT_SECONDS - 600)
    found = [f for f in findings(quiet) if f["rule"] == "silent"]
    assert len(found) == 1
    assert found[0]["seconds"] > monitor.SILENT_SECONDS


def test_a_job_talking_right_now_is_not_called_silent():
    assert "silent" not in rules(lines_for(REAL_SCORES[:5], start=NOW - 30))


# ---------------------------------------------------------------- staying quiet


def test_a_healthy_run_produces_nothing_at_all():
    healthy = [3.0 - 2.0 / (i + 1) for i in range(30)]   # rises, flattens
    assert findings(lines_for(healthy)) == []


def test_a_short_series_is_not_judged_however_bad_it_looks():
    # Five steps of a run that will take ten thousand. A trend measured there is
    # noise, and a monitor that fires in the first minute of every job is one
    # nobody reads twice.
    steep = [10.0, 8.0, 6.0, 4.0, 2.0]
    assert len(steep) < monitor.MIN_ROWS_TO_JUDGE
    assert "regression" not in rules(lines_for(steep))


def test_a_dip_smaller_than_the_threshold_is_left_alone():
    # Training is noisy. 5% down over twenty steps is a Tuesday.
    mild = [3.0 - 0.008 * i for i in range(20)]
    assert "regression" not in rules(lines_for(mild))


def test_a_log_with_no_metric_lines_at_all_is_not_a_finding():
    # A CPU image with no python3, and a training that writes its numbers once at
    # the end, both land here. Neither is a failure and neither should wake
    # anybody.
    plain = [f"2026-09-15T00:00:0{i}.000000000Z epoch {i} finished" for i in range(5)]
    assert [f for f in findings(plain, now=NOW) if f["rule"] != "silent"] == []


def test_nine_trainings_are_judged_one_by_one():
    # Eight healthy and one bad must report exactly one finding, naming the bad
    # one. Averaged together they would look fine, which is how the real run hid.
    lines = []
    for i in range(8):
        lines += lines_for([1.0 + 0.05 * s for s in range(20)], series=f"ok/{i}")
    lines += lines_for(REAL_SCORES, series="bad/one")
    found = [f for f in findings(lines) if f["rule"] == "regression"]
    assert len(found) == 1
    assert found[0]["series"] == "bad/one"


# ---------------------------------------------------------------- the message


def test_the_message_says_the_machine_was_stopped_when_it_was():
    # ★ A READER WHO THINKS THE PLATFORM DID NOT STOP IT WILL GO AND STOP SOMETHING
    # THAT IS ALREADY STOPPED, which on RunPod means a pod that has already
    # released its GPU and may not get one back.
    text = monitor.message_for("job-x", "exp", [{"rule": "nan", "detail": "NaN"}], "",
                               None, None, stop_state=monitor.STOP_DONE)
    assert "멈췄습니다" in text
    assert "resume" in text, "stopping without saying how to resume is a dead end"
    assert "계속 과금" not in text


def test_a_job_that_cannot_be_stopped_says_why_and_that_it_is_still_billing():
    # "cannot be stopped" with no reason reads as a platform fault. It is not: it
    # is a property of what was bought or how it was configured, and the reason
    # travels from `driver/common/stopcapability.py` rather than being re-worded
    # here -- two places writing the same refusal is two places to keep in step.
    reason = ("this job bought a spot instance, and a plain spot instance has no stopped "
              "state.")
    text = monitor.message_for("job-x", "exp", [{"rule": "nan", "detail": "NaN"}], "",
                               None, None, stop_state=monitor.STOP_IMPOSSIBLE,
                               stop_reason=reason)
    assert "멈출 수 없어서 계속 과금" in text
    assert "spot" in text
    # And it must name the only lever that IS available, with its cost.
    assert "cancel" in text and "잃습니다" in text


def test_the_message_says_nothing_was_stopped():
    # ★ A READER WHO THINKS THE PLATFORM ALREADY STOPPED THE JOB WILL NOT GO AND
    # STOP IT. The monitor cannot stop anything yet -- stopping today means
    # deleting, which returns the machine and loses the run -- so every message
    # says so.
    text = monitor.message_for("job-a24568ecfc16", "c3-job1-fix",
                               [{"rule": "silent", "detail": "nothing for 40 minutes"}],
                               "", None, None)
    assert "멈추지 않았습니다" in text
    assert "c3-job1-fix" in text and "job-a24568ecfc16" in text


def test_the_message_carries_the_money_when_it_is_known():
    # "Your job looks wrong" and "your job looks wrong and has spent $62" are
    # different messages, and the second one gets read first.
    text = monitor.message_for("job-x", "exp", [{"rule": "nan", "detail": "NaN"}],
                               "", 6.36, 9.74)
    assert "$61.95" in text


def test_an_explanation_is_included_when_there_is_one_and_omitted_when_not():
    findings_in = [{"rule": "nan", "detail": "NaN in score"}]
    assert "설명" in monitor.message_for("j", "n", findings_in, "설명 문장", None, None)
    plain = monitor.message_for("j", "n", findings_in, "", None, None)
    assert plain.count("\n\n") <= 2


# ---------------------------------------------------------------- never breaking


def test_a_model_that_cannot_be_reached_still_sends_the_numbers(monkeypatch):
    # The DM must go out whether or not a third party answers. An alert that
    # depends on an external service is an alert that is missing on the day that
    # service is down, which correlates with the days things go wrong.
    def refuse(*args, **kwargs):
        raise TimeoutError("upstage is slow today")

    monkeypatch.setattr(monitor.urllib.request, "urlopen", refuse)
    assert monitor.explain([{"rule": "nan"}], [], "a-key") == ""


def test_no_api_key_means_no_call_and_no_error(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a call was made with no credential")

    monkeypatch.setattr(monitor.urllib.request, "urlopen", explode)
    assert monitor.explain([{"rule": "nan"}], [], "") == ""


def test_slack_is_not_called_without_a_token_or_a_user(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("slack was called with nothing to call it with")

    monkeypatch.setattr(monitor.urllib.request, "urlopen", explode)
    assert monitor.notify("U123", "text", "") is False
    assert monitor.notify("", "text", "a-bot-token") is False


def test_an_owner_who_is_not_in_the_map_is_not_an_error(monkeypatch):
    monkeypatch.setenv("HYPERUN_SLACK_USER_MAP", json.dumps({"alice": "U01"}))
    assert monitor.slack_id_for("alice") == "U01"
    assert monitor.slack_id_for("nobody") == ""


def test_a_malformed_user_map_does_not_raise(monkeypatch):
    monkeypatch.setenv("HYPERUN_SLACK_USER_MAP", "{not json")
    assert monitor.slack_id_for("alice") == ""


def test_a_job_whose_log_cannot_be_read_is_skipped_rather_than_fatal():
    # The common case is a job that finished between the list and the read. A
    # monitor that dies on the first unreadable log reports nothing on the day
    # something is actually wrong.
    class Gone:
        def job_log_window(self, *args, **kwargs):
            raise RuntimeError("pods \"x-pod-0\" not found")

    assert monitor.check_one(Gone(), "ns", {"metadata": {"name": "x"}}, NOW) == []


def test_the_directory_is_fetched_before_it_is_read(monkeypatch, tmp_path):
    # ★ THE FIRST LIVE RUN DIED ON EXACTLY THIS. The gateway fetches the token
    # directory once in its lifespan and then serves for weeks; this process
    # starts from nothing every ten minutes with an empty /tmp, so a `load`
    # without a fetch raises
    #     TokenFileError: cannot read the token file at /tmp/hyperun-tokens.json
    # It is not a startup ordering detail -- it is the difference between a
    # CronJob that runs and one that has never run successfully.
    fetched: list = []
    target = tmp_path / "tokens.json"
    # A real directory, because `TokenStore` refuses an empty `tokens` array --
    # a server that admits nobody is a configuration error, not a quiet state.
    target.write_text(json.dumps({"tokens": [
        {"email": "alice@example.com", "user": "alice", "namespace": "ddps-alice", "team": "ddps"}]}),
        encoding="utf-8")

    monkeypatch.setenv("HYPERUN_TOKENS_PATH", str(target))
    monkeypatch.setenv("HYPERUN_RESULT_BUCKET", "a-bucket")
    monkeypatch.setattr(monitor.tokens_source, "fetch_to_file",
                        lambda path, sid="": fetched.append(path) or False)

    class NoJobs:
        def list_jobs(self, namespace):
            return []

    # `Cluster.connect()`, not `Cluster()`. The constructor takes two API clients
    # and the factory is what builds them from the pod's service account -- the
    # second thing the first live run died on (`TypeError: Cluster.__init__()
    # missing 2 required positional arguments`). Patching the factory rather than
    # the class is what makes this test notice if that ever changes back.
    monkeypatch.setattr(monitor.k8s.Cluster, "connect", staticmethod(lambda: NoJobs()))
    assert monitor.run_once() == 0
    assert fetched, "the directory was read without being fetched first"


# ---------------------------------------------------------------- the DM's shape


def _kinds(blocks):
    return [b["type"] for b in blocks]


def _text_of(blocks):
    import json as _json
    return _json.dumps(blocks, ensure_ascii=False)


REGRESSION = [{"rule": "regression", "series": "bank/adapters/AD/iter_1",
               "field": "score", "change_ratio": -0.249,
               "detail": "bank/adapters/AD/iter_1: score went the wrong way"}]


def test_the_headline_says_what_is_wrong_rather_than_that_something_is():
    # ★ "이(가) 이상합니다" is gone. It hid the particle behind a bracket, and it
    # said nothing: a reader who got a warning already knows something is wrong
    # and wants to know WHAT. The rule already knows, so the headline uses it.
    blocks = monitor.blocks_for("job-x", "exp", REGRESSION, "", None, None)
    head = blocks[0]["text"]["text"]
    assert "이(가)" not in head
    assert head == "학습이 제대로 되고 있지 않습니다"


@pytest.mark.parametrize("rule,expected", [
    ("crash", "오류"), ("nan", "깨졌"), ("silent", "아무 말"), ("regression", "학습이"),
])
def test_each_rule_has_its_own_headline(rule, expected):
    blocks = monitor.blocks_for("job-x", "exp", [{"rule": rule, "detail": "d"}],
                                "", None, None)
    assert expected in blocks[0]["text"]["text"]


def test_a_job_with_several_findings_gets_the_most_actionable_headline():
    # A crash explains every other rule that fired, so it leads. The order is
    # "what does the reader do first", not severity.
    both = [{"rule": "regression", "detail": "d"}, {"rule": "crash", "detail": "d"}]
    assert "오류" in monitor.blocks_for("j", "n", both, "", None, None)[0]["text"]["text"]


def test_the_dm_is_shaped_like_main_1():
    # header, then sections under `[ ... ]` labels, with dividers between. The
    # cost report in cloud-usage is that shape and the same person reads both in
    # the same Slack.
    blocks = monitor.blocks_for("job-x", "exp", REGRESSION, "설명", 6.36, 9.74)
    assert _kinds(blocks)[0] == "header"
    assert _kinds(blocks).count("divider") >= 3
    body = _text_of(blocks)
    for label in ("[ 지금까지 ]", "[ 점검이 찾은 것 ]", "[ AI 설명 ]", "[ 기계 ]"):
        assert label in body, f"{label} 절이 없다"


def test_the_ai_paragraph_is_in_its_own_labelled_section():
    # ★ THE LABEL IS THE BOUNDARY. Everything above it is measurement; this
    # paragraph is a model's prose. Run together, a reader cannot tell where the
    # numbers stop and the guessing starts.
    blocks = monitor.blocks_for("job-x", "exp", REGRESSION, "모델이 쓴 문장", None, None)
    labels = [i for i, b in enumerate(blocks)
              if b.get("text", {}).get("text") == "*[ AI 설명 ]*"]
    assert len(labels) == 1
    at = labels[0]
    assert blocks[at + 1]["text"]["text"] == "모델이 쓴 문장"
    # And the section says out loud that a model wrote it and did not decide it.
    assert blocks[at + 2]["type"] == "context"
    assert "판정은" in _text_of([blocks[at + 2]])


def test_no_ai_section_at_all_when_the_model_said_nothing():
    blocks = monitor.blocks_for("job-x", "exp", REGRESSION, "", None, None)
    assert "[ AI 설명 ]" not in _text_of(blocks)


def test_the_money_leads_because_it_decides_what_happens_next():
    blocks = monitor.blocks_for("job-x", "exp", REGRESSION, "", 6.36, 9.74)
    body = _text_of(blocks)
    assert "$61.95" in body
    assert body.index("[ 지금까지 ]") < body.index("[ 점검이 찾은 것 ]")


def test_the_plain_text_carries_the_same_facts_as_the_blocks():
    # Slack uses `text` for the notification preview. Somebody who reads only the
    # preview must not get a different story from somebody who opens it.
    text = monitor.message_for("job-x", "exp", REGRESSION, "설명", 6.36, 9.74)
    assert "학습이 제대로 되고 있지 않습니다" in text
    assert "$61.95" in text
    assert "[AI 설명]" in text


def test_blocks_are_sent_with_the_text_and_not_instead_of_it(monkeypatch):
    # A message with blocks and no text arrives as a silent push saying nothing.
    sent = {}

    class Fake:
        def __init__(self, body):
            self._body = body

        def read(self):
            import json as _json
            return _json.dumps(self._body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(request, timeout=None):
        import json as _json
        payload = _json.loads(request.data)
        if request.full_url.endswith("conversations.open"):
            return Fake({"ok": True, "channel": {"id": "D1"}})
        sent.update(payload)
        return Fake({"ok": True})

    monkeypatch.setattr(monitor.urllib.request, "urlopen", fake_open)
    assert monitor.notify("U1", "미리보기", "a-token",
                          blocks=[{"type": "divider"}]) is True
    assert sent["text"] == "미리보기"
    assert sent["blocks"] == [{"type": "divider"}]
