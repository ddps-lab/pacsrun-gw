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
# ★ THE PROMPT IS WRITTEN IN KOREAN AND CARRIES A BAD EXAMPLE, and both are there
# because of what each version actually produced. Two rounds of this:
#
#   round 1 (English instructions) -> 해당 훈련 시리즈의 *score* 컬럼이 ... 학습률
#                                     과다 설정이 가장 의심됩니다
#   round 2 (Korean, translationese named) -> 처음 다섯 걸음 평균 2.85 에서 ...
#
# `걸음` is `step` translated, and translating it is the mistake: a researcher
# reads `step` in tqdm's own output, in their trainer's config and in every
# tutorial, and cannot grep a Korean word for it. Round 2 also said `학습률` in
# one sentence and `learning_rate` in the next, so the same quantity had two
# names inside one answer.
#
# So the prompt now has a NAMED LIST of terms that stay English -- step, epoch,
# batch, loss, learning rate, gradient, checkpoint, optimizer, overfitting,
# warmup, scheduler -- and a rule that one answer uses one name for one thing. Asked in English for "at most
# four sentences in Korean", `solar-pro3` answered (2026-09-16, a real DM):
#
#   해당 훈련 시리즈의 *score* 컬럼이 목표 지표이며 ... 학습률 과다 설정이 가장 의심됩니다
#
# Three separate faults in one sentence. `시리즈` is "series" spelled phonetically,
# which is not a Korean word for anything and cannot be looked up. `*score*` is
# markdown the model added to a column name. `과다 설정` is a Sino-Korean compound
# nobody says -- the Korean for it is "너무 큽니다". None of that is a failure to
# understand the numbers; it is a failure of register, and the fix for register is a
# worked example of the register you want, not more instructions.
#
# WHY THE RULES ARE IN KOREAN. A model asked in English to write Korean translates,
# and translation is exactly where this went wrong. Asking in the target language
# gets the target language's own sentence shapes.
#
# AND IT IS STILL NEVER ASKED TO JUDGE. The findings are handed to it as settled.
PROMPT = """훈련 job 을 감시하는 중입니다. 자동 점검이 이미 "문제가 있다" 고 판정했습니다.
당신이 할 일은 다시 판정하는 것이 아니라, 그 판정을 job 을 낸 연구자에게 설명하는 것입니다.

점검이 찾은 것:
{findings}

그 학습이 스스로 남긴 숫자 (학습마다 처음 몇 줄과 마지막 몇 줄):
{series}

세 문장 안에 쓰십시오. 읽는 사람은 자기 모델은 잘 알지만 오늘 이 실행을 아직 안 봤습니다.
  1. 어느 값이 목표이고 그 값이 어떻게 움직였는지. 근거로 쓴 숫자를 그대로 적으십시오
  2. 그 움직임의 모양. 꾸준히 갔는지 한 번에 튀었는지, 언제부터인지
  3. 지금 확인해볼 것 한 가지

★ 정식 용어는 영어 그대로 씁니다. 번역하지 마십시오.
  step, epoch, batch, loss, learning rate, gradient, checkpoint, optimizer,
  overfitting, warmup, scheduler — 이것들은 이 분야의 이름입니다. 한국어로 옮기면
  읽는 사람이 자기 로그와 코드에서 그 단어를 다시 못 찾습니다.
      step 을 "걸음" 이라고 쓰지 마십시오. "5 step", "step 마다" 입니다.
      learning rate 를 "학습률" 로 바꿨다가 다음 문장에서 learning_rate 로 돌아가지
      마십시오. 한 답 안에서 한 이름만 씁니다.
      metric 을 "메트릭", series 를 "시리즈" 처럼 소리 나는 대로 옮기지 마십시오.
      열 이름과 파일 이름도 영어 그대로, 별표나 백틱 없이. score, loss,
      bank/adapters/AD/iter_1.

★ 그 밖의 문장 규칙
  * 번역투를 쓰지 마십시오. "해당 ~ 의", "~ 에 대하여", "~ 하는 것" 을 반복하지 마십시오.
  * 없는 한자어를 만들지 마십시오. "과다 설정" 이 아니라 "너무 큽니다".
  * "~ 쪽이 의심됩니다" 처럼 둘러 말하지 말고 "~ 가 너무 큽니다" 라고 쓰십시오.
  * "~ 로 보입니다", "~ 인 것으로 판단됩니다" 같은 보고서 말투를 피하고 "~ 입니다",
    "~ 같습니다" 로 끝내십시오.
  * 숫자는 단위와 함께 쓰십시오.
  * ★★ 원인을 단정하지 마십시오. 위에 있는 것은 어느 값이 어떻게 움직였는가 뿐이고,
    learning rate 도 batch size 도 optimizer 설정도 data 도 위에 없습니다. 그것들
    중 무엇이 원인인지는 이 숫자만으로 알 수 없습니다. "learning rate 가 너무 큽니다"
    라고 쓰면 연구자는 그것을 고치러 갑니다 — 원인이 다른 데 있으면 GPU 시간을 한 번 더
    태웁니다. 대신 "무엇을 확인해보십시오" 라고 쓰십시오.
  * ★ 위에 준 숫자만 쓰십시오. 없는 값을 지어내지 마십시오. learning rate 나 batch
    size 가 위에 없으면 그 값이 얼마인지 말하지 마십시오.
  * 이 실행이 괜찮다고 쓰지 마십시오. 점검이 이미 아니라고 했습니다.

이렇게 쓰십시오:
  bank/adapters/AD/iter_1 의 score 가 목표인데, 처음 5 step 평균 2.85 에서 마지막 5 step
  평균 2.14 로 24.9% 떨어졌습니다. 한 번 튄 것이 아니라 step 마다 0.027 씩 꾸준히
  내려갔습니다. learning rate 와 data 를 확인해보십시오.

여기서 원인을 하나로 찍지 않은 것에 주의하십시오. 위에 있는 것은 score 값뿐이고, 그것만으로
무엇이 그렇게 만들었는지는 알 수 없습니다.

이렇게 쓰지 마십시오:
  ... step 마다 0.027 씩 꾸준히 내려가서 learning rate 가 너무 큽니다. 로그에서 learning
  rate 를 확인하고 절반으로 줄여서 다시 돌려보세요.

이 문장이 나쁜 이유는 숫자를 지어내서가 아닙니다. learning rate 를 원인으로 단정했는데,
그렇게 판단할 근거가 위에 없기 때문입니다.

이렇게 쓰지 마십시오:
  해당 훈련 시리즈의 *score* 컬럼이 목표 지표이며, 처음 다섯 걸음 평균 2.85 에서 마지막
  다섯 걸음 평균 2.14 로 약 24% 감소하였습니다. 학습률 과다 설정이 가장 의심되는 것으로
  판단됩니다.
"""


def _rounded(row: dict) -> dict:
    """Every float in one row cut to four significant figures.

    Integers are left alone -- a step number is a step number -- and anything
    that is not a number passes through. Applied to what the MODEL sees, never
    to what the rules decided on: a threshold compared against a rounded value
    would fire differently from one compared against the real one.
    """
    out = {}
    for key, value in row.items():
        if isinstance(value, bool) or not isinstance(value, float):
            out[key] = value
            continue
        out[key] = float(f"{value:.4g}")
    return out


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
    # ★ ROUNDED BEFORE IT GOES OUT, and this is not cosmetic. A float like
    # 2.1399999999999997 or a slope of 0.02699203007518797 is what python's
    # repr gives, and the model quotes what it is given -- so the researcher's
    # Slack message reads like a machine talking to itself. Four significant
    # figures is more than any of these decisions needs.
    findings = [_rounded(f) for f in findings]
    trimmed = []
    for s in series[:4]:
        trimmed.append({
            "series": s.name,
            "fields": s.fields,
            "first_rows": [_rounded(r) for r in s.rows[:3]],
            "last_rows": [_rounded(r) for r in s.rows[-3:]],
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


# ★ WHAT HAPPENED TO THE MACHINE, WHICH IS THE LAST LINE OF EVERY MESSAGE AND THE
# ONE MOST LIKELY TO BE ACTED ON. A reader who thinks the platform already stopped
# the job will not go and stop it, and a reader who thinks it did not will go and
# stop something that is already stopped. So the message never leaves it implied,
# and the three states are kept apart by name rather than by a boolean:
#
#   STOP_DONE      the machine was paused. Billing is now storage only.
#   STOP_IMPOSSIBLE  it CANNOT be paused, and the reason is a property of what was
#                  bought or how it was configured -- Shadeform has no stop API at
#                  all, a spot instance has no stopped state, a RunPod pod with no
#                  volume would lose everything. The reason travels with it,
#                  because "cannot be stopped" with no reason reads as a fault.
#   STOP_NOT_TRIED   nothing was attempted. Today this is every message: the
#                  monitor tells a person and the person decides.
STOP_DONE = "done"
STOP_IMPOSSIBLE = "impossible"
STOP_NOT_TRIED = "not_tried"


def _stop_word(stop_state: str) -> str:
    """`stop` 이거나 `running`. 두 가지뿐이다.

    ★ 세 상태를 두 낱말로 줄인 것이고, 그 셋은 위에 그대로 남아 있다. 읽는 사람이 이
    줄에서 알고 싶은 것은 하나다 -- **기계가 지금 돌고 있는가.** 멈출 수 없어서 돌고
    있는 것과 아무도 안 멈춰서 돌고 있는 것은 이 질문에 같은 답이고, 둘을 갈라 쓰면
    한 줄짜리 자리에 이유가 들어가 눈에 안 들어온다. 이유는 [ 검토 사항 ] 이 말한다.

    돈은 바로 위 [ 진행 사항 ] 의 비용(추정)이 이미 말하고 있다. 그래서 이 줄은
    "계속 과금됩니다" 를 되풀이하지 않는다.
    """
    return "stop" if stop_state == STOP_DONE else "running"


# 어느 rule 이 먼저 제목이 되는가. 한 job 이 여러 개를 동시에 낼 수 있고, 제목은
# 하나뿐이다. 순서는 "읽는 사람이 먼저 할 일" 순이지 심각도 순이 아니다 -- crash 는
# 로그를 열면 답이 있고, NaN 은 다시 돌려야 하고, 침묵은 기다릴지 말지를 정해야 하고,
# 추세는 판단이 필요하다.
_HEADLINES = (
    ("crash", "job 이 오류를 내고 멈춰 있습니다"),
    ("nan", "학습 숫자가 깨졌습니다"),
    ("silent", "job 이 아무 말도 하지 않습니다"),
    ("regression", "학습이 제대로 되고 있지 않습니다"),
)


def _headline(findings: list[dict]) -> str:
    """제목 한 줄.

    ★ "이(가) 이상합니다" 를 그만 쓴다. 그 문장은 조사를 괄호로 피해 간 자리가
    눈에 걸리고, 무엇보다 아무것도 말하지 않는다 -- 읽는 사람은 이미 경고를 받았고
    알고 싶은 것은 "무엇이" 다. rule 이 그것을 이미 알고 있으므로 제목에 그대로 쓴다.
    """
    kinds = {f.get("rule") for f in findings}
    for rule, text in _HEADLINES:
        if rule in kinds:
            return text
    return "job 을 확인해 주십시오"


def blocks_for(job_id: str, name: str, findings: list[dict], explanation: str,
               cost_per_hour: float | None, hours: float | None,
               stop_state: str = "", stop_reason: str = "") -> list[dict]:
    """Slack Block Kit 으로 짠 DM.

    ★ Main 1 과 같은 짜임이다. header 하나, 그 아래로 `[ ... ]` 이름표를 단 절들,
    절 사이에 divider. cloud-usage 의 비용 보고서가 그 모양이고, 같은 사람이 같은
    Slack 에서 읽으므로 두 보고서가 서로 다른 모양일 이유가 없다.

    ★★ AI 가 쓴 문장은 자기 절에 따로 둔다. 앞 절들은 숫자와 규칙이 만든 사실이고
    이 절은 모델이 쓴 글이라, 한 덩어리로 섞으면 어디까지가 측정이고 어디부터가
    추측인지 읽는 사람이 가를 수 없다. `[ AI 설명 ]` 이라는 이름표가 그 경계이고,
    그것으로 충분하다 -- 절 안에 "이건 모델이 썼습니다" 를 한 줄 더 달았다가 뺐다.
    이름표가 이미 말하고 있는 것을 다시 말하면 읽는 사람은 그 줄을 건너뛴다.

    Args:
        stop_state: STOP_DONE / STOP_IMPOSSIBLE / STOP_NOT_TRIED.
        stop_reason: IMPOSSIBLE 일 때 `stopcapability` 가 준 이유.

    Returns:
        Block Kit 블록 목록. `notify` 가 이것과 fallback 문장을 함께 보낸다.
    """
    out: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": _headline(findings), "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{name}*  ·  `{job_id}`"}},
    ]

    if cost_per_hour and hours:
        # 돈이 제일 먼저 온다. "뭔가 이상하다" 와 "뭔가 이상한데 $62 를 썼다" 는
        # 읽는 사람이 다음에 하는 행동이 다른 두 문장이다.
        out += [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*[ 진행 사항 ]*"}},
            {"type": "section", "fields": [
                # `Hour`, not 시간. Main 4 in cloud-usage says Hour for the same
                # reason the user gave there: a person reading this against the
                # cost report should meet one word, not two.
                {"type": "mrkdwn", "text": f"*Hour*\n`{hours:.1f}`"},
                {"type": "mrkdwn", "text": f"*비용(추정)*\n`${cost_per_hour * hours:,.2f}`"},
            ]},
        ]

    out += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*[ 검토 사항 ]*"}},
    ]
    for finding in findings:
        out.append({"type": "section",
                    "text": {"type": "mrkdwn", "text": "• " + finding["detail"]}})

    if explanation:
        out += [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*[ AI 설명 ]*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": explanation}},
        ]

    # 제목 밑의 `*이름*  ·  \`job-...\`` 와 같은 모양이다. 절 이름과 문장을 쓰던
    # 자리인데, 여기서 답할 것은 한 낱말이라 한 줄로 줄였다.
    out += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*중단 여부*  ·  `{_stop_word(stop_state or STOP_NOT_TRIED)}`"}},
    ]
    return out


def message_for(job_id: str, name: str, findings: list[dict], explanation: str,
                cost_per_hour: float | None, hours: float | None,
                stop_state: str = STOP_NOT_TRIED, stop_reason: str = "") -> str:
    """같은 내용의 평문. Slack 의 알림 미리보기와, 블록을 못 그리는 곳을 위한 것.

    블록이 본문이고 이것이 대체본이다. 그래도 같은 사실을 같은 순서로 담는다 --
    미리보기만 보고 넘기는 사람이 다른 이야기를 읽으면 안 된다.
    """
    head = f"{_headline(findings)}  |  {name} ({job_id})"
    if cost_per_hour and hours:
        head += f"\n지금까지 {hours:.1f}시간, 약 ${cost_per_hour * hours:,.2f}"
    parts = [head, "\n".join(f"• {f['detail']}" for f in findings)]
    if explanation:
        parts.append(f"\n[AI 설명] {explanation}")
    parts.append(f"중단 여부: {_stop_word(stop_state)}")
    return "\n".join(parts)


def notify(slack_user_id: str, text: str, token: str, timeout: float = 10.0,
           blocks: list[dict] | None = None) -> bool:
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
        # `text` TRAVELS EVEN WHEN BLOCKS DO. Slack uses it for the notification
        # preview and for any client that cannot draw blocks, and a message with
        # blocks and no text arrives as a silent push saying nothing.
        payload = {"channel": opened["channel"]["id"], "text": text}
        if blocks:
            payload["blocks"] = blocks
        sent = call("chat.postMessage", payload)
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
    cluster = k8s.Cluster.connect()
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
            explanation = explain(findings, series, upstage)
            job_id = labels.get("ddpsrun.io/job-id", name)
            text = message_for(job_id, display, findings, explanation, None, None)
            blocks = blocks_for(job_id, display, findings, explanation, None, None)
            if notify(slack_id_for(owner), text, slack, blocks=blocks):
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
