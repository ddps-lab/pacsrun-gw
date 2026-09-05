"""The text `GET /v1/explain` returns.

WHY THIS IS A SEPARATE FILE. It is prose, it will be edited often, and it is the
one thing in this package that a non-programmer might reasonably want to change.
Keeping it out of `main.py` means editing it never risks the routing.

WHO READS IT. A coding agent that has a shell, this URL, and nothing else — see
`docs/07-agent-skill.md`. So it is written for a reader with no context: it says
what the service does, what it will not do, and exactly which calls to make, in
order. It does not assume the reader knows Kubernetes, PACSrun, or that a GPU is
being rented from anybody.

WHAT IT MUST NOT SAY. No namespace names, no ServiceAccount names, no bucket
name, no `PACSRUN_*` variable. Those are internal (`docs/03-api.md`, first rule
of the "응답 규칙" / response-rules section) and this endpoint has no token, so
it is the most public thing here.

Grep anchor: DDPSRUN-EXPLAIN
"""

EXPLAIN_TEXT = """\
ddpsrun — submit a batch job, get results back.

WHAT IT DOES
  You describe a container image and a command. We rent a machine with the GPU
  you asked for, run your command on it, hand the output back, and give the
  machine up. You never see the machine and you do not need an account with any
  cloud provider.

WHAT IT DOES NOT DO
  It is not a shell and not a notebook. There is no way to log into the machine
  while the job runs. A job that needs a human halfway through will not work.
  Nothing is installed for you: whatever your command needs must be in the image
  or fetched by the command itself.

THE CALLS, IN ORDER
  GET  /v1/schema                     the exact shape of a request
  POST /v1/estimate                   how long, how much, which GPU. Runs nothing
  POST /v1/validate                   what is wrong with this job. Runs nothing
  POST /v1/jobs                       submit. returns {job_id, name, result_path}
  GET  /v1/jobs/{job_id}              phase, message, which GPU, restart count
  GET  /v1/jobs/{job_id}/logs         output. add ?follow=true to keep streaming

  All four POST routes take the SAME body, so you check a job and then submit
  that exact job with nothing rewritten in between.

  Everything except /v1/explain and /v1/schema needs a header:
      Authorization: Bearer <your token>

A SUBMISSION, WHOLE
  POST /v1/jobs
  {
    "name": "my-finetune",
    "image": "runpod/pytorch:1.1.0-rc.154-cu1281-torch291-ubuntu2404",
    "args": ["bash", "-lc", "python train.py --epochs 4"],
    "env": {"EPOCHS": "4"},
    "secrets": ["GITHUB_PAT"],
    "gpu": {"vram_gb": 48, "count": 1},
    "expected_hours": 8
  }

WHAT YOU DO NOT SEND, AND MUST NOT TRY TO
  Where the output goes, which identity the job runs as, and which slice of
  storage it may write to are all decided from your token. There is no field for
  any of them. This is what keeps two people's results apart.

SECRETS
  Never put a credential in "env" — that value is stored in the clear and shows
  up in logs and backups. Put its NAME in "secrets" instead. The value is
  already stored on our side; we look it up and hand it to your container
  without it passing through this API. Ask an operator to store a new one.
  A name that is not stored is refused, and the error lists what is available.

GPUS
  Ask by memory, "gpu": {"vram_gb": 48}, or by exact model,
  "gpu": {"name": "L40S"}. One or the other, never both, never neither.
  Sizes as the vendor prints them on the card: 24 an L4, 48 an L40S, 80 an H100.
  Omit "gpu" entirely for a job that needs no GPU.

PHASES
  Pending     accepted, nothing bought yet
  Starting    a machine is being obtained and the container is coming up
  Running     your command is executing
  Recovering  the machine was lost; another is being obtained. This is normal
              on rented capacity and your job has not failed
  Succeeded   your command exited 0
  Failed      it did not. "message" says why

LOGS
  Your command's own stdout, with our runner's bookkeeping lines removed. There
  is a delay before any line exists: the container has to be pulled first, and a
  large image can take several minutes. Until then the log call answers 404 with
  "the job has not started a container", which is not an error.

RESULTS
  "result_path" in the submit response is an S3 location. Anything your command
  writes there is yours to collect. Write it there yourself; nothing is copied
  for you.

BEFORE YOU SUBMIT
  Call /v1/estimate and /v1/validate. They run nothing and cost nothing.

  /v1/estimate needs facts we cannot read out of an image, under "training":
  how many pairs your dataset holds, how many epochs, the average length of one
  response in tokens, and your --max-len. Without the cap there is no memory
  answer; without the length there is no runtime answer, and it will say so
  rather than invent one. "confidence": "unknown" is a real answer. The last
  time this guessed at a combination nobody had measured, it was 96% out.

  /v1/validate takes the same body plus, optionally, the text of your run.sh as
  "script". Four of its checks need that text. Its "not_checked" list says what
  no check could look at, so a clean result is not a complete one.

WHAT IS NOT BUILT YET
  No listing of available GPUs and prices. No cancel. No upload endpoint, so
  your command must fetch its own code and data.
"""
