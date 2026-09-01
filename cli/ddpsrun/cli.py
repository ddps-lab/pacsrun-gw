"""The `ddpsrun` command: argument parsing, and deciding what to print.

END-TO-END FLOW of one `ddpsrun submit -f job.yaml`:

  1. `main()` parses the arguments and dispatches to `cmd_submit`.
  2. `config.load()` finds the server URL and the token, from the environment or
     from `~/.config/ddpsrun/config.json`.
  3. `build_submit_body()` reads the file, applies any flag overrides on top,
     and produces the request body. It does NOT validate: the server owns that
     judgement, and a second copy here would eventually disagree with it.
  4. `Client.submit()` POSTs it.
  5. The job id, the phase and the result path are printed, or the whole JSON
     when `--json` was given.

THE COMMANDS, AND WHY EACH EXISTS

  login / logout    store and remove the token. Stage 1 authentication is a
                    static token an operator hands out, so `login` takes one
                    rather than opening a browser. When Cognito lands
                    (`docs/08-plan.md` open item 4) only this command changes.
  explain / schema  ask the SERVER what it is and what it accepts, rather than
                    printing something baked in here that can go stale. This is
                    what makes the tool usable by a coding agent that has read
                    no documentation (`docs/07-agent-skill.md`).
  estimate          how long, how much, which GPU. Submits nothing.
  validate          what is wrong with the job. Submits nothing. Exits 1 when
                    something would actually stop it, so a script can gate a
                    submit on it.
  submit            the point of the whole thing.
  status            phase, message, which GPU, how many restarts.
  watch             GPU usage and training progress, read out of the job's own
                    log. Nothing is stored anywhere for this.
  stats             what your team has spent. Aggregate only: being on a team
                    does not let you read a member's jobs.
  logs              output, optionally followed.

WHAT IS NOT HERE. No `gpus`. Answering it needs a vendor API key in the server
and a catalogue cache, and a CLI that answered it locally would be the second
source of truth this design exists to avoid.

EXIT CODES, because a script will read them:
  0  it worked
  1  the server refused, or could not be reached
  2  the command was wrong, or there are no credentials

Grep anchor: DDPSRUN-CLI
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import time
import sys
from typing import Any

from . import browser_login
from . import config
from .client import Client, ServerError

EXIT_OK = 0
EXIT_SERVER = 1
EXIT_USAGE = 2


def job_arguments() -> argparse.ArgumentParser:
    """The arguments that describe a job, shared by three subcommands.

    WHY THIS IS A PARENT PARSER RATHER THAN THREE COPIES. `estimate`,
    `validate` and `submit` must accept exactly the same job description, so
    that a user checks a job and then submits THAT job with nothing rewritten
    in between. Three copies would drift, and the drift would show up as a
    validate that passes something the submit refuses.

    Returns:
        A parser with `add_help=False`, to be passed as a `parents=` entry.
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("-f", "--file", help="a YAML or JSON file describing the job")
    shared.add_argument("--name", help="a name for your own benefit")
    shared.add_argument("--image", help="container image to run")
    shared.add_argument(
        "--arg", action="append", default=[], dest="args_", metavar="ARG",
        help="one argument to the container. Repeat it, in order.",
    )
    shared.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="a non-secret environment variable. Repeatable.",
    )
    shared.add_argument(
        "--secret", action="append", default=[], metavar="NAME",
        help="the NAME of a stored secret to inject. Never the value. Repeatable.",
    )
    shared.add_argument("--gpu-vram", type=int, metavar="GB", help="minimum GPU memory, e.g. 48")
    shared.add_argument("--gpu-name", metavar="MODEL", help="exact GPU model, e.g. L40S")
    shared.add_argument(
        "--gpu-count", type=int, metavar="N", help="how many GPUs PER POD (default 1)"
    )
    shared.add_argument(
        "--capacity-type", choices=["on-demand", "spot"],
        help="how the machine is bought. YOU decide this. on-demand costs more and is "
        "not taken away; spot is cheaper and can be reclaimed mid-run. Run "
        "`ddpsrun estimate` first — it recommends one and says why. submit refuses "
        "without it rather than choosing for you.",
    )
    shared.add_argument(
        "--parallelism", type=int, metavar="N",
        help="how many pods run at once (default 1). They are independent workers that never "
        "talk to each other. With --gpu-count this is how a job fills a multi-GPU machine: "
        "--parallelism 8 --gpu-count 1 may land 4 pods on each of two 4-GPU boxes.",
    )
    shared.add_argument("--cpus", help='CPU request, e.g. "4"')
    shared.add_argument("--memory", help='memory request, e.g. "16Gi"')
    shared.add_argument(
        "--expected-hours", type=float, help="your own guess at the runtime, in hours"
    )
    # The facts the server cannot read out of a container image. Only estimate
    # and validate use them today, but submit accepts them too so that one file
    # works for all three.
    shared.add_argument("--pairs", type=int, help="how many training pairs your dataset holds")
    shared.add_argument("--epochs", type=int, help="how many passes over the dataset")
    shared.add_argument(
        "--row-tokens", type=int, metavar="N",
        help="average length of ONE response, in tokens. Without it there is no "
        "runtime estimate.",
    )
    shared.add_argument("--cap", type=int, help="--max-len, which decides peak memory")
    shared.add_argument("--batch-size", type=int, help="per_device_train_batch_size (default 1)")
    shared.add_argument("--grad-accum", type=int, help="gradient_accumulation_steps (default 8)")
    shared.add_argument(
        "--resumable", action="store_true",
        help="your job can restart from a checkpoint",
    )
    shared.add_argument(
        "--script", metavar="PATH",
        help="your run.sh. Four more validate checks become available with it. "
        "It is read and sent, never stored.",
    )
    return shared


def build_parser() -> argparse.ArgumentParser:
    """Assemble the command line.

    Returns:
        A parser whose every `help` string is written for someone who has not
        read anything else. This is the only documentation most users will see.
    """
    parser = argparse.ArgumentParser(
        prog="ddpsrun",
        description="Submit a batch job to a GPU we rent for you, and get the results back.",
        epilog="Run `ddpsrun explain` for the full description, straight from the server.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add_json_flag(target: argparse.ArgumentParser) -> None:
        """`--json` goes on each subcommand that has something to print.

        Not on the top-level parser: argparse would require it BEFORE the
        subcommand (`ddpsrun --json status X`), which is not the order anyone
        types, and a subparser redefining it would silently reset it to False.
        """
        target.add_argument(
            "--json", action="store_true",
            help="print raw JSON instead of a human summary. Use this from a script.",
        )

    login = sub.add_parser("login", help="sign in and store the result")
    login.add_argument("--server", required=True, help="the gateway URL, e.g. https://run.example")
    login.add_argument(
        "--token",
        help="skip the browser and use this token. For CI and scripts, which "
        "have nobody to sign in. Omitting it opens a browser when the server "
        "supports that, and prompts otherwise so it stays out of shell history.",
    )

    sub.add_parser("logout", help="delete the stored token")
    sub.add_parser("explain", help="what this tool is and how to use it (asks the server)")
    sub.add_parser("schema", help="the exact shape of a submit request (asks the server)")

    job = job_arguments()

    estimate = sub.add_parser(
        "estimate", parents=[job],
        help="how long it will take, what it will cost, which GPU. Submits nothing",
        description="Nothing is submitted. An answer of `unknown` is a real answer: "
        "the last time we estimated a combination we had never measured, we were 96% out.",
    )
    add_json_flag(estimate)

    validate = sub.add_parser(
        "validate", parents=[job],
        help="what is wrong with this job. Submits nothing",
        description="Nothing is submitted. Pass --script to unlock four more checks.",
    )
    add_json_flag(validate)

    submit = sub.add_parser(
        "submit", parents=[job], help="submit a job",
        description="Give a YAML or JSON file, or build the request from flags, or both. "
        "Flags win over the file.",
    )
    add_json_flag(submit)

    status = sub.add_parser("status", help="how a job is doing")
    status.add_argument("job_id")
    add_json_flag(status)

    watch = sub.add_parser(
        "watch", help="GPU usage and training progress",
        description="Read out of the job's own log. Nothing is stored, so what "
        "you can see goes back as far as the log does.",
    )
    watch.add_argument("job_id")
    watch.add_argument(
        "--window", type=int, default=3600, metavar="SECONDS",
        help="how far back to read (default 3600, max 86400)",
    )
    add_json_flag(watch)

    team_stats = sub.add_parser(
        "stats", help="what your team has spent",
        description="Aggregate only. Being on a team does not let you read a member's jobs.",
    )
    add_json_flag(team_stats)

    logs = sub.add_parser("logs", help="a job's output")
    logs.add_argument("job_id")
    logs.add_argument(
        "-f", "--follow", action="store_true",
        help="keep printing as new lines arrive. The server cannot stream, so this "
        "asks again every few seconds and prints what is new.",
    )
    logs.add_argument(
        "--interval", type=float, default=6.0, metavar="SECONDS",
        help="how often to ask, with --follow (default 6)",
    )

    return parser


def load_job_file(path: str) -> dict[str, Any]:
    """Read a job description from YAML or JSON.

    Args:
        path: the file to read. `.json` is parsed as JSON, anything else as
            YAML — and YAML is a superset of JSON, so a `.txt` holding JSON
            works too.

    Returns:
        The parsed mapping.

    Raises:
        SystemExit: the file is unreadable or is not a mapping. Exiting here
            rather than raising keeps the error one line instead of a traceback.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc

    try:
        if path.endswith(".json"):
            document = json.loads(text)
        else:
            import yaml

            document = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - json and yaml raise different types
        raise SystemExit(f"{path} does not parse: {exc}") from exc

    if not isinstance(document, dict):
        raise SystemExit(f"{path} must contain a mapping, not a {type(document).__name__}")
    return document


def parse_env_pairs(pairs: list[str]) -> dict[str, str]:
    """Turn `--env KEY=VALUE` arguments into a mapping.

    Args:
        pairs: the raw strings.

    Returns:
        A dict. A value containing `=` is kept whole, because
        `--env ARGS=--lr=1e-5` is a real thing people write.

    Raises:
        SystemExit: a pair has no `=` at all.
    """
    result: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise SystemExit(f"--env {pair!r} must be KEY=VALUE")
        result[key] = value
    return result


def build_submit_body(args: argparse.Namespace) -> dict[str, Any]:
    """Assemble the request body from a file and the flags on top of it.

    Args:
        args: the parsed `submit` arguments.

    Returns:
        The body to POST. Nothing is validated here — the server decides what is
        acceptable, and a second copy of that judgement in the CLI would drift
        away from it. The one exception is the shape of `--env`, which has to be
        parsed before it can be sent at all.

    Raises:
        SystemExit: neither a file nor the two fields the server requires.
    """
    body: dict[str, Any] = load_job_file(args.file) if args.file else {}

    if args.name:
        body["name"] = args.name
    if args.image:
        body["image"] = args.image
    if args.args_:
        body["args"] = args.args_
    if args.env:
        # Merged, not replaced: a file may carry the ten stable values and a
        # flag override the one that changes between runs.
        merged = dict(body.get("env") or {})
        merged.update(parse_env_pairs(args.env))
        body["env"] = merged
    if args.secret:
        body["secrets"] = sorted(set(body.get("secrets") or []) | set(args.secret))
    if args.cpus:
        body["cpus"] = args.cpus
    if args.memory:
        body["memory"] = args.memory
    if args.expected_hours is not None:
        body["expected_hours"] = args.expected_hours
    if getattr(args, "parallelism", None) is not None:
        body["parallelism"] = args.parallelism
    if getattr(args, "capacity_type", None):
        body["capacity_type"] = args.capacity_type

    # A GPU is asked for in exactly one of two styles, by memory or by model.
    # TWO CASES THAT LOOK ALIKE AND ARE NOT:
    #   file says one style, a flag says the other  -> the flag is the newer
    #       intent, so it replaces the file's style.
    #   BOTH flags on the same command line         -> the user contradicted
    #       themselves. Picking one would send a job to a GPU they did not ask
    #       for, so this stops. Found 2026-08-31 by running the CLI against the
    #       server: the two branches used to erase each other and whichever ran
    #       last silently won.
    if args.gpu_vram and args.gpu_name:
        raise SystemExit(
            "--gpu-vram and --gpu-name ask for a GPU in two different ways. "
            "Give one: --gpu-vram 48 for 'at least 48 GB', --gpu-name L40S for "
            "that exact model."
        )
    if args.gpu_vram or args.gpu_name or args.gpu_count:
        gpu = dict(body.get("gpu") or {})
        if args.gpu_vram:
            gpu["vram_gb"] = args.gpu_vram
            gpu.pop("name", None)
        if args.gpu_name:
            gpu["name"] = args.gpu_name
            gpu.pop("vram_gb", None)
        if args.gpu_count:
            gpu["count"] = args.gpu_count
        body["gpu"] = gpu

    # The facts the server cannot read out of a container image. They live under
    # `training` in the request, and a flag overrides the file the same way
    # everything else does.
    training = dict(body.get("training") or {})
    for flag, field in (
        ("pairs", "pairs"), ("epochs", "epochs"), ("row_tokens", "row_tokens"),
        ("cap", "cap"), ("batch_size", "batch_size"), ("grad_accum", "grad_accum"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            training[field] = value
    if getattr(args, "resumable", False):
        training["resumable"] = True
    if training:
        body["training"] = training

    # `--script run.sh` sends the file's TEXT, not its path. The server has no
    # way to read a file on the user's laptop.
    script_path = getattr(args, "script", None)
    if script_path:
        try:
            with open(script_path, "r", encoding="utf-8") as handle:
                body["script"] = handle.read()
        except OSError as exc:
            raise SystemExit(f"cannot read {script_path}: {exc}") from exc

    if not body.get("name") or not body.get("image"):
        raise SystemExit(
            "a job needs at least a name and an image. Give them with --name and "
            "--image, or in a file with -f. `ddpsrun schema` prints every field."
        )
    return body


def refreshed(credentials: config.Credentials) -> config.Credentials:
    """Renew the stored id_token when it is about to expire.

    DDPSRUN-CLI-REFRESH. A Cognito id_token lives an hour. Without this, a
    command run 61 minutes after `ddpsrun login` fails with 401 and the person
    has no idea why. With a refresh token stored, the renewal is silent.

    Args:
        credentials: what `config.load` returned.

    Returns:
        The same credentials, or ones carrying a fresh id_token. A static token
        has no `exp` and comes back untouched, as does anything that cannot be
        renewed — the request then fails on its own and says so, which is a
        better error than one from in here.
    """
    if not credentials.refresh_token:
        return credentials
    try:
        payload = credentials.token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode())
        expiry = float(claims["exp"])
    except Exception:
        return credentials
    # 60 seconds of margin, so a token does not expire between this check and
    # the request it is about to be used on.
    if time.time() < expiry - 60:
        return credentials

    try:
        settings = browser_login.login_config(credentials.server)
        if not settings.get("enabled"):
            return credentials
        tokens = browser_login.exchange(settings, {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
        })
    except browser_login.LoginError:
        return credentials

    renewed = config.Credentials(
        server=credentials.server,
        token=tokens.id_token or credentials.token,
        # A refresh does not mint a new refresh token, so keep the one we have.
        refresh_token=tokens.refresh_token or credentials.refresh_token,
    )
    config.save(renewed)
    return renewed


def client_from_config() -> Client:
    """Build a client, or exit 2 saying how to log in."""
    try:
        credentials = config.load()
    except config.NotLoggedIn as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from exc
    credentials = refreshed(credentials)
    return Client(credentials.server, credentials.token)


def cmd_login(args: argparse.Namespace) -> int:
    """Store the server address and the token.

    The token is read with `getpass` when it was not given on the command line,
    so it does not end up in the shell's history file.
    """
    server = args.server.rstrip("/")
    refresh = ""

    if args.token:
        token = args.token.strip()
    else:
        # DDPSRUN-CLI-LOGIN. With no --token, try the browser first. A server
        # that has no user pool says so and we fall back to the prompt, which is
        # exactly what this command did before Cognito existed.
        try:
            tokens = browser_login.login(server)
            token, refresh = tokens.id_token, tokens.refresh_token
        except browser_login.LoginError as exc:
            if "does not have browser sign-in" not in str(exc):
                print(str(exc), file=sys.stderr)
                return EXIT_SERVER
            token = getpass.getpass("token: ").strip()

    if not token:
        print("no token given", file=sys.stderr)
        return EXIT_USAGE

    path = config.save(config.Credentials(
        server=server, token=token, refresh_token=refresh))
    print(f"saved to {path}")

    # Prove the credentials work now, rather than at the user's first real
    # command. `status` on an id that cannot exist returns 404 when the token is
    # good and 401 when it is not, which is exactly the distinction we want.
    #
    # The id has letters in it on purpose: twelve digits in a row is what an AWS
    # account number looks like, and this repository's CI refuses those on
    # sight (`.github/workflows/ci.yml`).
    try:
        Client(args.server, token).status("job-ffffffffffff")
    except ServerError as exc:
        message = str(exc)
        if "no such job" in message:
            print("token accepted")
        else:
            print(f"warning: the server did not accept this: {message}", file=sys.stderr)
    return EXIT_OK


def cmd_logout(_: argparse.Namespace) -> int:
    """Delete the stored credentials."""
    print("logged out" if config.forget() else "nothing stored")
    return EXIT_OK


def cmd_explain(_: argparse.Namespace) -> int:
    """Print the server's own description of itself."""
    print(client_from_config().explain(), end="")
    return EXIT_OK


def cmd_schema(args: argparse.Namespace) -> int:
    """Print the JSON Schema of a submit request."""
    print(json.dumps(client_from_config().schema(), indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_submit(args: argparse.Namespace) -> int:
    """Submit a job and print where its output will go."""
    body = build_submit_body(args)
    result = client_from_config().submit(body)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"submitted  {result['job_id']}")
        print(f"results    {result['result_path']}")
        print(f"follow     ddpsrun logs {result['job_id']} --follow")
    return EXIT_OK


def cmd_estimate(args: argparse.Namespace) -> int:
    """Print how long a job will take, what it will cost, and which GPU it wants."""
    body = build_submit_body(args)
    result = client_from_config().estimate(body)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK

    hours, cost, gpu = result["hours"], result["cost_usd"], result["gpu"]
    if result.get("steps"):
        print(f"  steps          {result['steps']:,}")
    if hours.get("low") is not None:
        print(f"  time           {hours['low']} - {hours['high']} h  [{hours['confidence']}]")
    else:
        # `unknown` is a real answer, and printing a blank instead of saying so
        # is how a user ends up assuming zero.
        print(f"  time           unknown  [{hours['confidence']}]")
    if cost.get("low") is not None:
        print(f"  cost           ${cost['low']} - ${cost['high']}")
    print(f"  basis          {result['basis']}")
    print(f"  GPU            {gpu.get('recommended') or 'none'}"
          f" ({gpu.get('recommended_vram_gb')} GB), logits peak {gpu['peak_logits_gib']} GiB")
    print(f"                 {gpu['reason']}")
    print(f"  capacity type  {result['capacity_type']}   "
          f"<- pass --capacity-type {result['capacity_type']} when you submit")
    print(f"                 {result['capacity_reason']}")
    for warning in result.get("warnings", []):
        print(f"  note           {warning}")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    """Print what is wrong with a job, without submitting it.

    Returns:
        0 when nothing is an error, 1 when something is. A script can gate a
        submit on this.
    """
    body = build_submit_body(args)
    result = client_from_config().validate(body)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK if result["ok"] else EXIT_SERVER

    marker = {"error": "[error]  ", "warning": "[warning]", "info": "[info]   "}
    for finding in result.get("findings", []):
        print(f"{marker.get(finding['level'], '[?]      ')} {finding['code']}")
        print(f"  {finding['message']}")
        if finding.get("fix"):
            print(f"  fix: {finding['fix']}")
        print()
    for line in result.get("not_checked", []):
        print(f"[not checked] {line}")
    print()
    print("Nothing blocking." if result["ok"] else "Something is blocking this job.")
    return EXIT_OK if result["ok"] else EXIT_SERVER


def cmd_status(args: argparse.Namespace) -> int:
    """Print one job's state."""
    view = client_from_config().status(args.job_id)
    if args.json:
        print(json.dumps(view, indent=2, ensure_ascii=False))
        return EXIT_OK

    # A job the controller has not looked at yet has an empty phase. Saying
    # "accepted" is truer than printing a blank.
    print(f"{view['job_id']}  {view.get('name', '')}")
    print(f"  phase      {view.get('phase') or 'accepted, not yet started'}")
    if view.get("gpu"):
        vendor = f" ({view['vendor']})" if view.get("vendor") else ""
        print(f"  running on {view['gpu']}{vendor}")
    if view.get("recovery_count"):
        # Not a failure. Rented capacity is taken back, and the job is restarted.
        print(f"  restarts   {view['recovery_count']} (the machine was reclaimed)")
    if view.get("message"):
        print(f"  message    {view['message']}")
    if view.get("result_path"):
        print(f"  results    {view['result_path']}")
    return EXIT_OK


def bar(percent: float, width: int = 20) -> str:
    """Draw a progress bar.

    Args:
        percent: 0 to 100.
        width: how many characters wide.

    Returns:
        A string of filled and empty blocks.

    Example:
        >>> bar(50, width=10)
        '#####-----'
    """
    filled = int(round(width * max(0.0, min(100.0, percent)) / 100))
    return "#" * filled + "-" * (width - filled)


def cmd_watch(args: argparse.Namespace) -> int:
    """Print a job's GPU usage and how far the training has got."""
    result = client_from_config().metrics(args.job_id, args.window)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK

    progress = result.get("progress")
    if progress:
        print(f"  training       {bar(progress['percent'])}  "
              f"{progress['step']:,} / {progress['total_steps']:,} steps  "
              f"({progress['percent']}%)")
        print(f"                 {progress['seconds_per_step']} s/step, "
              f"{progress['elapsed']} elapsed, {progress['remaining']} remaining")
        # `steady` False means too few steps have run for this to be worth
        # quoting, so it is labelled rather than printed as a fact.
        settled = "" if progress["steady"] else "  (rate has not settled yet)"
        print(f"  projected      {progress['projected_total_hours']} h{settled}")

    gpu = result.get("latest_gpu")
    if gpu:
        print(f"  GPU util       {bar(gpu['utilization_percent'])}  "
              f"{gpu['utilization_percent']}%")
        print(f"  GPU memory     {bar(gpu['memory_percent'])}  "
              f"{gpu['memory_used_mib']:,} / {gpu['memory_total_mib']:,} MiB "
              f"({gpu['memory_percent']}%)")
        print(f"  temp, power    {gpu['temperature_c']} C, {gpu['power_w']} W")
        print(f"  samples        {len(result.get('gpu_series', []))}, "
              f"last {result['window_seconds']}s")

    if result.get("note"):
        print(f"  note           {result['note']}")
    return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    """Print this caller's team figures."""
    result = client_from_config().stats()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK

    print(f"team {result['team'] or '(none)'}")
    if result.get("members"):
        print(f"  {'member':<14}{'jobs':>6}{'ok':>5}{'failed':>8}{'running':>9}"
              f"{'GPU h':>9}{'spend':>10}")
        for m in result["members"]:
            print(f"  {m['user']:<14}{m['jobs']:>6}{m['succeeded']:>5}{m['failed']:>8}"
                  f"{m['running']:>9}{m['gpu_hours']:>9}{('$' + str(m['cost_usd'])):>10}")
        print(f"  {'total':<14}{result['jobs']:>6}{'':>5}{'':>8}{'':>9}"
              f"{result['gpu_hours']:>9}{('$' + str(result['cost_usd'])):>10}")
    if result.get("note"):
        print(f"  note           {result['note']}")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    """Print a job's output, once or repeatedly.

    WHY THIS POLLS INSTEAD OF STREAMING. The server runs as a Lambda function
    and one execution is capped at 15 minutes, while a training run is thirty
    hours. So a window is read, printed, and asked for again.

    The only state kept is `since`, the timestamp of the last line printed.
    The server remembers nothing, which is what lets a fresh execution answer
    every request.
    """
    client = client_from_config()
    # A window several times the interval, so one slow round trip does not lose
    # lines. Capped at the server's own limit.
    window = min(3600, max(5, int(args.interval * 5)))
    since: str | None = None

    while True:
        result = client.log_window(args.job_id, since=since, window_seconds=window)
        for line in result["lines"]:
            # The timestamp is bookkeeping for the next request, not something a
            # user asked to read, so it is dropped on the way to the terminal.
            print(line.split(" ", 1)[1] if " " in line else line)
        if result.get("last_timestamp"):
            since = result["last_timestamp"]
        if not args.follow:
            return EXIT_OK
        time.sleep(args.interval)


COMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "explain": cmd_explain,
    "schema": cmd_schema,
    "estimate": cmd_estimate,
    "validate": cmd_validate,
    "submit": cmd_submit,
    "status": cmd_status,
    "watch": cmd_watch,
    "stats": cmd_stats,
    "logs": cmd_logs,
}


def main(argv: list[str] | None = None) -> int:
    """The entry point `pip install ddpsrun` puts on the PATH.

    Args:
        argv: arguments without the program name. Defaults to `sys.argv[1:]`.

    Returns:
        The exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    try:
        return COMMANDS[args.command](args)
    except SystemExit as exc:
        # `build_submit_body` and `client_from_config` raise SystemExit with a
        # one-line message already printed, because a traceback for "you forgot
        # --image" helps nobody. Catching it here keeps `main`'s contract — it
        # RETURNS an exit code — true for every path, which is what lets a test
        # call it directly and what lets another program import it.
        code = exc.code
        if isinstance(code, str):
            print(code, file=sys.stderr)
            return EXIT_USAGE
        return EXIT_USAGE if code is None else int(code)
    except ServerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_SERVER
    except KeyboardInterrupt:
        # Ctrl-C during `logs --follow` is how a user stops watching. It is not
        # a failure and it does not touch the job, which keeps running.
        print()
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
