# ddpsrun

**Submit a GPU job. Get results back. No `kubectl`, no cloud account, no VM to babysit.**

`ddpsrun` is the front door to PACSrun, a Kubernetes-native batch job system that rents GPUs from
whichever vendor is cheapest for the shape you asked for — AWS, GCP or RunPod — runs your code
there, and puts the results in object storage. You describe the job; you never see the machine.

```console
$ ddpsrun login <token>
$ ddpsrun estimate -f job.yaml
$ ddpsrun submit -f job.yaml
$ ddpsrun logs <job-id> --follow
```

## Why it exists

A researcher who wants a GPU for two hours has three bad options today: get a cloud account and
learn its console, get `kubectl` access to somebody's cluster, or ask a person. Each of those
teaches infrastructure to somebody whose job is not infrastructure.

The alternative here is that **a batch job is the unit**. You hand over a job description and a
program. The platform decides which machine to rent, rents it, runs your code, brings the output
home, and hands the machine back. When it goes wrong you get an exit code and a sentence, not a
half-configured VM.

## Install

```console
pip install ddpsrun
```

Two dependencies, `requests` and `PyYAML`, and that is deliberate: this package installs next to
your research code, so every extra dependency is one that can fight with something you have
pinned. No `click`, no `rich`, no `pydantic`.

Python 3.9 or newer.

## Configure

`ddpsrun` needs a server URL and a token. Ask whoever runs your deployment for both.

```console
$ export DDPSRUN_SERVER=https://<your-gateway-url>
$ ddpsrun login <token>
```

`login` writes them to `~/.config/ddpsrun/config.json` with mode 0600, or to
`$XDG_CONFIG_HOME/ddpsrun` when that is set. For CI, where a file is the wrong shape, set
`DDPSRUN_TOKEN` in the environment and skip `login` entirely.

## The commands

| | |
|---|---|
| `login` / `logout` | store and remove the server URL and token |
| `explain` | what this deployment can do, in prose — run it first |
| `schema` | the job description's fields and their types |
| `estimate` | what a job would cost and how long it would take, before you spend anything |
| `validate` | check a job description without submitting it |
| `submit` | submit it |
| `status` / `watch` | where a job is now; `watch` follows until it ends |
| `logs` | your program's own output, `--follow` to stream |
| `stats` | GPU utilisation, memory, temperature and power while the job runs |
| `cancel` | stop a job and hand its machine back |

Exit codes are contractual: **0** it worked, **1** the server refused or a `validate` finding would
stop the job, **2** the command was wrong or there are no credentials.

## Writing a job

A job description is YAML:

```yaml
image: nvcr.io/nvidia/pytorch:24.10-py3
command: ["/bin/bash", "-c"]
args:
  - |
    python train.py --epochs 3
resources:
  gpus:
    name: L4
    count: 1
resultPath: s3://<your-bucket>/runs/my-experiment/
```

Run `ddpsrun schema` for every field and `ddpsrun explain` for what your deployment allows.

**Do not guess the GPU or the runtime.** `ddpsrun estimate` answers both from measured data, and
guessing is how a job ends up on a card several times more expensive than it needed.

## Using it from an AI coding agent

This repository ships a [Claude Code](https://claude.com/claude-code) plugin. It teaches an agent
five steps: read what the deployment allows, read your repository against the script contract,
estimate rather than assume, validate and stop on a finding, and submit only after you approve.

```
/plugin marketplace add ddps-lab/pacsrun-gw
/plugin install ddpsrun
```

The plugin's reference documents are in [`agent/references/`](agent/references/). The one worth
reading yourself is [`script-contract.md`](agent/references/script-contract.md): a list of the ways
a long training run dies in its last minute, each one taken from a run that did.

## What this is not

- **Not an interactive machine.** There is no SSH and no shell into a running job. You submit a
  program and read its output. If you need to poke at a live container, this is the wrong tool
  today.
- **Not a scheduler you host.** `ddpsrun` is a client. Somebody has to run the gateway and the
  PACSrun operator; see [`docs/`](docs/) if that somebody is you.
- **Not free of limits.** A job's credentials for writing results last twelve hours, which is
  AWS's hard maximum for a role session and not a setting anyone can raise. A run longer than that
  must checkpoint and resume, and the driver warns half an hour before the deadline.

## Repository layout

| | |
|---|---|
| `cli/` | the `ddpsrun` package published to PyPI |
| `server/` | the gateway: authenticates users, talks to Kubernetes |
| `agent/` | the Claude Code plugin — one skill, four reference documents |
| `ui/` | a small static web front end (`index.html`, `app.js`, `style.css`) |
| `terraform/` | the deployment: Lambda, Cognito, S3, CloudFront |
| `docs/` | seventeen design documents, numbered in reading order |

## Development

```console
$ pip install -e 'cli/[dev]'      # the client
$ pip install -e 'server/[dev]'   # the gateway
$ pytest cli/tests server/tests
```

Both halves live in one repository so the two sides of an API change land in one commit. They share
nothing else: the client never imports the Kubernetes libraries, and the server never imports the
browser login flow.

**No account identifiers in this repository.** Documents and code use placeholders —
`<ACCOUNT_ID>`, `<RESULT_BUCKET>` — and CI checks every push for AWS account numbers, access keys,
GitHub and RunPod tokens, private keys, and Google OAuth secrets.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
