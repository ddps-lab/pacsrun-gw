"""Notice while a job is still running that it has stopped being worth paying for.

END-TO-END FLOW of one check:

  1. A CronJob runs `python -m ddpsrun_server.monitor` on an interval. It is the
     SAME image the gateway pod runs, with a different entrypoint, so everything
     below is code that already exists and is already tested.
  2. `k8s.list_jobs` is called once per namespace in the directory, and jobs not
     in a running phase are dropped.
  3. For each, `k8s.job_log_window` reads the log and `metrics.scan` turns it
     into GPU readings, a progress line, and one `MetricSeries` per training.
  4. `findings_for` applies the rules below. They are arithmetic and comparisons.
     Nothing in this step asks a model anything.
  5. When something fires, `explain` shows the numbers to Upstage and asks for a
     sentence, and `notify` sends it to the job's owner as a Slack DM.
  6. Nothing is stopped. See WHAT THIS DOES NOT DO.

★ THE DIVISION OF LABOUR, WHICH IS THE DESIGN DECISION IN THIS FILE. The verdict
is arithmetic; the AI writes prose about a verdict already reached. That split is
a measurement, not a preference. Asked on 2026-09-15 to judge a real series whose
slope is -0.0269 per step and whose last five steps average 25% below its first
five, `solar-pro3` answered "the run is learning and improving": it had found a
mid-run peak and called it the end. The day before, given a log tail with no loss
in it at all, it reported a steady loss. Both answers are fluent and both are
wrong, and a rule that fires on a number cannot be either.

What the model IS good at, measured the same day: given the same rows with the
column names hidden, it correctly picked `score` as the objective out of nine
numeric fields. That is the job it is given here -- naming and explaining, never
deciding.

WHY A CronJob AND NOT A LOOP IN THE GATEWAY. The gateway answers requests; a
watcher that lives inside it would be duplicated by every replica, would restart
with every deploy, and would make a slow scan into a slow request. A CronJob has
its own schedule, its own failure, and its own log, and Kubernetes already knows
how to not run two of them at once (`concurrencyPolicy: Forbid`).

WHAT THIS DOES NOT DO, and the omission is deliberate. **It does not stop
anything.** Stopping a job today means deleting it, which returns the rented
machine and loses the run; there is no pause. Until `PacsJob` grows a `Stopped`
phase and a resume path, a monitor that could stop would be a monitor that can
only destroy. It tells a person, and the person decides.

WHAT IT COSTS. One log window per running job per tick, which is the same call
the screen makes when somebody opens a job. The AI call happens only when a rule
has already fired: `solar-pro3` is $0.15 per million input tokens and $0.60 per
million output, and a check carrying twenty rows measured 1,124 input and about
100 output tokens, so $0.00023 with VAT. A job that fires a rule every tick for a
day costs about two cents; the job it is watching costs $6.36 an hour.

Grep anchor: HYPERUN-MONITOR
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
import urllib.error
import urllib.request

from . import auth, config, k8s, metrics, tokens_source

logger = logging.getLogger("hyperun.monitor")

# Phases a job can still be helped out of. `Recovering` is included: the machine
# was reclaimed and the run is being restarted, so it is still costing money and
# still worth watching.
RUNNING_PHASES = ("Pending", "Starting", "Running", "Recovering")

# ------------------------------------------------------------------ the rules
#
# Every number below is a threshold somebody has to defend, so each one says what
# it is defending against. They are deliberately LOOSE. A monitor that cries wolf
# gets muted, and a muted monitor is worth less than none -- so the rules fire on
# shapes that are hard to argue with rather than on anything suspicious.

# How far back each check reads. Six hours rather than the hour a screen reads,
# because a trend measured over ten minutes of a nine-hour run is noise. The
# server caps `window_seconds` at seven days.
WINDOW_SECONDS = int(os.environ.get("HYPERUN_MONITOR_WINDOW_SECONDS", "21600"))

# Rule 1, SILENCE. The log has not gained a line in this long. PACSrun's driver
# already has its own stall detector at 3,600 s (`facts/silent-stall-detector.md`
# in the PACSrun repo) which DELETES the pod; this is deliberately shorter and
# only tells somebody, so a person hears about it before the platform acts.
SILENT_SECONDS = int(os.environ.get("HYPERUN_MONITOR_SILENT_SECONDS", "1800"))

# Rule 2, NO PROGRESS. The step number has not moved in this long, while the log
# is still producing lines. Different from silence: the job is talking and not
# advancing, which is what a deadlocked dataloader looks like.
STUCK_STEP_SECONDS = int(os.environ.get("HYPERUN_MONITOR_STUCK_SECONDS", "3600"))

# Rule 3, THE OBJECTIVE IS GOING THE WRONG WAY. The last window of a series
# averages this much worse than its first. 0.15 rather than something tighter
# because training is noisy and a 5% dip over a short run means nothing; the run
# that prompted this work was 25% out.
REGRESSION_RATIO = float(os.environ.get("HYPERUN_MONITOR_REGRESSION_RATIO", "0.15"))

# ...and a series shorter than this is not judged at all. Ten rows is where a
# head/tail comparison stops overlapping itself enough to mean something.
MIN_ROWS_TO_JUDGE = int(os.environ.get("HYPERUN_MONITOR_MIN_ROWS", "10"))

# Rule 4, A BROKEN NUMBER. NaN or an infinity anywhere in a series. No threshold:
# one is enough, and unlike the others it is not a matter of degree.

# Rule 5, A STACK TRACE. These strings in the log. Kept short and specific: a
# training script that PRINTS the word "error" while handling one correctly must
# not wake anybody at 3am.
CRASH_MARKERS = ("Traceback (most recent call last)", "CUDA out of memory",
                 "torch.OutOfMemoryError", "NCCL error", "Killed process")

# Which way is better for a field whose name says so. Everything else is judged
# only on `has_nan`, because the server does not know whether a researcher wants
# their `kl` to go up or down and guessing would produce confident nonsense.
LOWER_IS_BETTER = ("loss", "error", "perplexity", "ppl", "nll")
HIGHER_IS_BETTER = ("score", "reward", "acc", "accuracy", "f1", "bleu", "rouge",
                    "precision", "recall", "win_rate")


def _direction(field_name: str) -> int:
    """+1 when higher is better, -1 when lower is, 0 when the name does not say.

    Args:
        field_name: the field as the training wrote it, e.g. `train_loss`.

    Returns:
        The direction, by substring match on the lowercased name. Substring and
        not equality because a field is `train_loss`, `loss_policy`, `eval_acc`
        rather than a bare word.
    """
    low = field_name.lower()
    for word in HIGHER_IS_BETTER:
        if word in low:
            return 1
    for word in LOWER_IS_BETTER:
        if word in low:
            return -1
    return 0


def findings_for(reading: metrics.Metrics, lines: list[str], now: float,
                 last_line_at: float | None) -> list[dict]:
    """What is wrong with one job, by arithmetic alone.

    Args:
        reading: what `metrics.scan` made of the job's log window.
        lines: the same window's raw lines, for the crash-marker scan.
        now: unix time, passed in so tests do not have to wait.
        last_line_at: unix time of the newest log line, or None when the window
            was empty.

    Returns:
        Zero or more findings, each a dict with `rule`, `detail` and the numbers
        that produced it. A dict rather than a class because it goes straight
        into a JSON body for the AI and into a Slack message, and a second
        representation would be a second thing to keep in step.
    """
    found: list[dict] = []

    # Rule 5 first: a stack trace explains every other rule that is about to
    # fire, so it should lead the message rather than trail it.
    for line in lines:
        for marker in CRASH_MARKERS:
            if marker in line:
                found.append({"rule": "crash", "marker": marker,
                              "detail": f"the log contains {marker!r}"})
                break
        if found:
            break

    if last_line_at is not None:
        quiet = now - last_line_at
        if quiet > SILENT_SECONDS:
            found.append({
                "rule": "silent", "seconds": round(quiet),
                "detail": f"nothing has been printed for {round(quiet / 60)} minutes",
            })

    for series in reading.metric_series:
        if series.row_count < MIN_ROWS_TO_JUDGE:
            continue
        for field_name, trend in series.trends.items():
            if trend.has_nan:
                found.append({
                    "rule": "nan", "series": series.name, "field": field_name,
                    "detail": f"{series.name}: {field_name} contains NaN or infinity",
                })
                continue
            direction = _direction(field_name)
            if direction == 0 or trend.change_ratio is None:
                continue
            # A negative product means the field moved opposite to the direction
            # its name asks for: a loss that rose, a score that fell.
            moved_wrong = -direction * trend.change_ratio
            if moved_wrong > REGRESSION_RATIO:
                found.append({
                    "rule": "regression", "series": series.name, "field": field_name,
                    "change_ratio": trend.change_ratio, "slope": trend.slope,
                    "head": trend.head, "tail": trend.tail,
                    "rows": series.row_count,
                    "detail": (f"{series.name}: {field_name} went the wrong way, "
                               f"{trend.head:.4g} to {trend.tail:.4g} over "
                               f"{series.row_count} steps"),
                })
    return found


# ------------------------------------------------------------------ the sentence


UPSTAGE_URL = "https://api.upstage.ai/v1/chat/completions"
UPSTAGE_MODEL = os.environ.get("HYPERUN_UPSTAGE_MODEL", "solar-pro3")

# ★ WHAT THE MODEL IS ASKED, WORD FOR WORD, AND WHAT IT IS NOT. It is given the
# verdict and told to explain it. It is never asked whether the run is healthy --
# that question has already been answered by `findings_for`, and answering it
# again is exactly where this model failed twice on 2026-09-15.
PROMPT = """A training job is being watched. Automated checks have already decided that
something is wrong; your job is NOT to re-judge that, it is to explain it to the
researcher who submitted the job.

What the checks found:
{findings}

The training's own numbers (first rows and last rows of each series):
{series}

Write at most four sentences in Korean, for a researcher who knows their own
model but has not looked at this run today:
  1. which column looks like the objective, and what it did,
  2. what the most likely cause is, naming the specific numbers you used,
  3. one concrete thing they could check.
Do not say the run is fine. The checks have already said it is not."""


def explain(findings: list[dict], series: list[metrics.MetricSeries],
            api_key: str, timeout: float = 20.0) -> str:
    """Ask Upstage to turn the numbers into a sentence a person can act on.

    Args:
        findings: what `findings_for` returned. Non-empty.
        series: the trainings' own numbers, for context.
        api_key: the Upstage credential, from the environment.
        timeout: seconds to wait. Short: the DM must go out whether or not this
            answers, so a slow model must not hold up the alert.

    Returns:
        The model's text, or "" when it could not be reached. THE EMPTY STRING IS
        A SUPPORTED ANSWER, not an error: `notify` sends the findings either way,
        and a check that failed because a third party was slow must not become a
        job nobody hears about.
    """
    if not api_key:
        return ""
    trimmed = []
    for s in series[:4]:
        trimmed.append({
            "series": s.name,
            "fields": s.fields,
            "first_rows": s.rows[:3],
            "last_rows": s.rows[-3:],
            "steps": [s.first_step, s.last_step],
        })
    body = json.dumps({
        "model": UPSTAGE_MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(
            findings=json.dumps(findings, ensure_ascii=False, indent=2),
            series=json.dumps(trimmed, ensure_ascii=False, indent=2))}],
        "max_tokens": 400,
    }).encode("utf-8")
    request = urllib.request.Request(
        UPSTAGE_URL, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read())
        return answer["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        logger.warning("could not reach the model: %s", exc)
        return ""


# ------------------------------------------------------------------ the message


def message_for(job_id: str, name: str, findings: list[dict], explanation: str,
                cost_per_hour: float | None, hours: float | None) -> str:
    """The Slack DM, assembled from what is known rather than from a template.

    The money goes in because it is the number that decides what the reader does
    next. "Your job looks wrong" and "your job looks wrong and has spent $62 so
    far" are different messages.
    """
    head = f":warning: *{name}* (`{job_id}`) 이(가) 이상합니다."
    if cost_per_hour and hours:
        head += f"\n지금까지 {hours:.1f}시간, 약 ${cost_per_hour * hours:.2f}."
    body = "\n".join(f"• {f['detail']}" for f in findings)
    parts = [head, body]
    if explanation:
        parts.append(f"\n{explanation}")
    # Said in every message rather than assumed: a reader who thinks the platform
    # already stopped the job will not go and stop it.
    parts.append("\n_아무것도 멈추지 않았습니다. 계속 과금됩니다._")
    return "\n".join(parts)


def notify(slack_user_id: str, text: str, token: str, timeout: float = 10.0) -> bool:
    """Send one Slack DM. Returns whether it went.

    Written here with urllib rather than importing cloud-usage's `send_dm`,
    because that module lives in another repository and this image must not grow
    a dependency on it. The two calls it makes are the same two.
    """
    if not token or not slack_user_id:
        return False

    def call(method: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    try:
        opened = call("conversations.open", {"users": slack_user_id})
        if not opened.get("ok"):
            logger.warning("slack refused to open a DM: %s", opened.get("error"))
            return False
        sent = call("chat.postMessage",
                    {"channel": opened["channel"]["id"], "text": text})
        if not sent.get("ok"):
            logger.warning("slack refused the message: %s", sent.get("error"))
            return False
        return True
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        logger.warning("could not reach slack: %s", exc)
        return False


def slack_id_for(owner: str) -> str:
    """The Slack user id for a job's owner, from `HYPERUN_SLACK_USER_MAP`.

    The map is a JSON object of `{"<owner label>": "U01234567"}`. It is the same
    shape cloud-usage's `IAM_SLACK_USER_MAP` uses, so an operator who has already
    built one can paste it across.

    Returns:
        The id, or "" when this person is not in the map -- which is not an
        error. A finding with nobody to send it to is still logged, and the log
        is what an operator reads when they wonder why they heard nothing.
    """
    try:
        table = json.loads(os.environ.get("HYPERUN_SLACK_USER_MAP", "{}"))
    except ValueError:
        logger.warning("HYPERUN_SLACK_USER_MAP is not valid JSON; nobody will be messaged")
        return ""
    return str(table.get(owner, "")) if isinstance(table, dict) else ""


# ------------------------------------------------------------------ one pass


def _last_line_time(lines: list[str]) -> float | None:
    """Unix time of the newest log line.

    Every line arrives prefixed with the RFC 3339 stamp the apiserver put on it
    (`job_log_window` asks for `timestamps=True`), so this reads the prefix
    rather than guessing from the content.
    """
    for line in reversed(lines):
        stamp = line.split(" ", 1)[0]
        try:
            import datetime
            moment = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return moment.timestamp()
        except ValueError:
            continue
    return None


def check_one(cluster, namespace: str, job: dict, now: float) -> list[dict]:
    """Read one job's log and return what is wrong with it.

    Separated from `run_once` so a test can drive a single job through the whole
    path with a fake cluster and no environment at all.
    """
    name = job.get("metadata", {}).get("name", "")
    try:
        lines = cluster.job_log_window(namespace, name, WINDOW_SECONDS, 10000)
    except Exception as exc:  # noqa: BLE001 - the cluster raises several types
        # A job whose pod has already gone is the common case here, and it is not
        # an error: the job finished between the list and the read.
        logger.info("could not read %s/%s: %s", namespace, name, exc)
        return []
    reading = metrics.scan(lines, WINDOW_SECONDS)
    return findings_for(reading, lines, now, _last_line_time(lines))


def run_once() -> int:
    """One pass over every running job. Returns how many were messaged about.

    Failure of ONE job's check never stops the others: a monitor that dies on the
    first unreadable log is a monitor that reports nothing on the day something
    is actually wrong.
    """
    # ★ FETCH THE DIRECTORY FIRST. The gateway does this in its lifespan and then
    # serves requests for weeks; this process starts from nothing every ten
    # minutes with an empty /tmp, so without it the very first pass dies on
    #     TokenFileError: cannot read the token file at /tmp/hyperun-tokens.json
    # which is what the first live run did (2026-09-15). The same function the
    # gateway calls, so the two cannot disagree about who exists.
    #
    # Settings is built FIRST and the path taken from it, rather than reading the
    # environment again here. `Settings.from_env` already knows which of the two
    # variable names this deployment uses (HYPERUN-ENV-RENAME), and a second
    # reader with its own fallback list is a second thing to keep in step.
    settings = config.Settings.from_env()
    tokens_source.fetch_to_file(pathlib.Path(settings.tokens_path))
    store = auth.TokenStore.load(settings.tokens_path)
    cluster = k8s.Cluster()
    upstage = os.environ.get("HYPERUN_UPSTAGE_API_KEY", "").strip()
    slack = os.environ.get("HYPERUN_SLACK_BOT_TOKEN", "").strip()
    now = time.time()

    messaged = 0
    for namespace in sorted(store.all_namespaces()):
        try:
            jobs = cluster.list_jobs(namespace)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not list %s: %s", namespace, exc)
            continue
        for job in jobs:
            phase = job.get("status", {}).get("phase", "")
            if phase not in RUNNING_PHASES:
                continue
            name = job.get("metadata", {}).get("name", "")
            labels = job.get("metadata", {}).get("labels", {})
            try:
                findings = check_one(cluster, namespace, job, now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("check failed for %s/%s: %s", namespace, name, exc)
                continue
            if not findings:
                continue

            owner = labels.get("ddpsrun.io/owner", "")
            display = labels.get("ddpsrun.io/name", name)
            logger.warning("%s/%s (%s): %s", namespace, name, owner,
                           "; ".join(f["detail"] for f in findings))

            lines = cluster.job_log_window(namespace, name, WINDOW_SECONDS, 10000)
            series = metrics.scan(lines, WINDOW_SECONDS).metric_series
            text = message_for(labels.get("ddpsrun.io/job-id", name), display, findings,
                               explain(findings, series, upstage), None, None)
            if notify(slack_id_for(owner), text, slack):
                messaged += 1
    return messaged


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        count = run_once()
    except Exception:  # noqa: BLE001
        logger.exception("the monitor pass failed")
        return 1
    logger.info("pass complete, %d job(s) messaged about", count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
