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
  submit            the point of the whole thing.
  status            phase, message, which GPU, how many restarts.
  logs              output, optionally followed.

WHAT IS NOT HERE. No `gpus`, no `validate`, no `estimate` — the server has no
route for them yet, and a CLI that answered them locally would be the second
source of truth this design exists to avoid.

EXIT CODES, because a script will read them:
  0  it worked
  1  the server refused, or could not be reached
  2  the command was wrong, or there are no credentials

Grep anchor: DDPSRUN-CLI
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from . import config
from .client import Client, ServerError

EXIT_OK = 0
EXIT_SERVER = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    """Assemble the command line.

    Returns:
        A parser whose every `help` string is written for someone who has not
        read anything else — this is the only documentation most users will see.
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
            "--json",
            action="store_true",
            help="print raw JSON instead of a human summary. Use this from a script.",
        )

    login = sub.add_parser("login", help="store the server address and your token")
    login.add_argument("--server", required=True, help="the gateway URL, e.g. https://run.example")
    login.add_argument(
        "--token",
        help="your token. Omit it and you will be prompted, which keeps it out of "
        "your shell history.",
    )

    sub.add_parser("logout", help="delete the stored token")
    sub.add_parser("explain", help="what this tool is and how to use it (asks the server)")
    sub.add_parser("schema", help="the exact shape of a submit request (asks the server)")

    submit = sub.add_parser(
        "submit",
        help="submit a job",
        description="Give a YAML or JSON file, or build the request from flags, or both — "
        "flags win over the file.",
    )
    submit.add_argument("-f", "--file", help="a YAML or JSON file describing the job")
    submit.add_argument("--name", help="a name for your own benefit")
    submit.add_argument("--image", help="container image to run")
    submit.add_argument(
        "--arg",
        action="append",
        default=[],
        dest="args_",
        metavar="ARG",
        help="one argument to the container. Repeat it, in order.",
    )
    submit.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a non-secret environment variable. Repeatable.",
    )
    submit.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="NAME",
        help="the NAME of a stored secret to inject. Never the value. Repeatable.",
    )
    submit.add_argument("--gpu-vram", type=int, metavar="GB", help="minimum GPU memory, e.g. 48")
    submit.add_argument("--gpu-name", metavar="MODEL", help="exact GPU model, e.g. L40S")
    submit.add_argument("--gpu-count", type=int, metavar="N", help="how many GPUs (default 1)")
    submit.add_argument("--cpus", help='CPU request, e.g. "4"')
    submit.add_argument("--memory", help='memory request, e.g. "16Gi"')
    submit.add_argument(
        "--expected-hours", type=float, help="your own guess at the runtime, in hours"
    )
    add_json_flag(submit)

    status = sub.add_parser("status", help="how a job is doing")
    status.add_argument("job_id")
    add_json_flag(status)

    logs = sub.add_parser("logs", help="a job's output")
    logs.add_argument("job_id")
    logs.add_argument(
        "-f", "--follow", action="store_true", help="keep printing as new lines arrive"
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

    if not body.get("name") or not body.get("image"):
        raise SystemExit(
            "a job needs at least a name and an image. Give them with --name and "
            "--image, or in a file with -f. `ddpsrun schema` prints every field."
        )
    return body


def client_from_config() -> Client:
    """Build a client, or exit 2 saying how to log in."""
    try:
        credentials = config.load()
    except config.NotLoggedIn as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from exc
    return Client(credentials.server, credentials.token)


def cmd_login(args: argparse.Namespace) -> int:
    """Store the server address and the token.

    The token is read with `getpass` when it was not given on the command line,
    so it does not end up in the shell's history file.
    """
    token = args.token or getpass.getpass("token: ")
    if not token.strip():
        print("no token given", file=sys.stderr)
        return EXIT_USAGE

    path = config.save(config.Credentials(server=args.server.rstrip("/"), token=token.strip()))
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


def cmd_logs(args: argparse.Namespace) -> int:
    """Print a job's output."""
    for line in client_from_config().logs(args.job_id, follow=args.follow):
        print(line)
    return EXIT_OK


COMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "explain": cmd_explain,
    "schema": cmd_schema,
    "submit": cmd_submit,
    "status": cmd_status,
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
