---
name: ddpsrun
description: Use when someone wants to run a training, fine-tuning or batch GPU job from their own repository and does not have kubectl, a kubeconfig, or a cloud account — building the run.sh, asking what it will cost and how long it will take, checking it before it runs, submitting it, and following it afterwards. Also use for reading back a running job's progress, GPU usage, logs and results.
---

# ddpsrun — someone's repository into a running GPU job

You turn a lab member's repository and intent into a **submitted job**, and you never
guess anything the server can answer. The measurements, the memory arithmetic and the
seven pitfalls are on the server; your part is reading their repository and asking.

**Language: answer lab members in Korean.** Code, commands, file names and log lines
stay in English.

## Step 0 — ask the tool, not your memory

Run these first. They are the current truth; anything written down goes stale.

```bash
ddpsrun explain     # what this is, what it will not do, what is not built yet
ddpsrun schema      # the exact request shape, generated from the server's own model
ddpsrun secrets     # which names `secrets:` accepts on THIS deployment
```

**`secrets` is a word, not a value, and you must not invent one.** A submit
request's `secrets: ["GITHUB_PAT"]` asks the server to open its own vault under
that name; the server refuses a name it does not hold, and the value never
travels through you or through the request. `ddpsrun secrets` prints the names
that work, marking which the deployment holds and which this namespace
registered itself.

### ★ If a name they need is missing, THEY type the value. You never see it.

`ddpsrun secret-set <NAME>` stores a value in their own namespace. Give them the
command to run and stop there:

```bash
ddpsrun secret-set HF_TOKEN --from-file /path/to/token.txt
# or, typed straight in and never written to disk:
ddpsrun secret-set HF_TOKEN     # reads stdin, Ctrl-D to finish
```

**NEVER ASK THEM TO PASTE IT TO YOU, and never put it in a command you run.** A
secret in this conversation is in the transcript; a secret in a command line is
in their shell history, in `ps` output for every other user on that machine, and
in any terminal recording. That is three copies nobody meant to make, in places
nobody thinks to clear — and rotating a credential is a great deal more work
than typing it once. The command above has no argument for the value for exactly
this reason.

**For a TEMPORARY credential, ask them to add `--expires-at`.** A federation
token lasts 36 hours; without the date, the only way to learn it has run out is
a job that fails at the call that needs it, hours in, on a rented machine — which
is what happened on 2026-09-08. With it, `validate` refuses the submit and the
GPU is never rented. `ddpsrun secrets` also marks a name as past its date.

If the value belongs to the whole lab rather than one namespace, say so and tell
them an operator stores it in the cluster instead. Either way, **do not put it in
`env`** — that sits in the job spec, and in every log and backup of it.

If `ddpsrun` is missing, `pip install ddpsrun`. If it says `not logged in`, tell the
user to run `ddpsrun login --server <url>` and stop — you must not ask for their token.

## Step 1 — read their repository before writing anything

**Read `../../references/script-contract.md` before you write a line of run.sh.** It is
the rule set, it is fifteen rules long, and every one of them came from a job that
broke. The paths in this document are relative to THIS file: the references sit two
levels up, at the plugin root (`<plugin>/references/`), not beside SKILL.md.

The sixteen, so you know which to open:

| # | rule | when it matters |
|---|---|---|
| 1 | check their documentation's paths against the repository's real layout | always |
| 2 | find the pairs of flags that must share one variable | always |
| 3 | the script makes the outputs the commands do not | always |
| 4 | put the cheap checkpoints first | always |
| 5 | prove the dataset, the model and the result path are reachable BEFORE training | always |
| 6 | upload what exists whenever a stage dies (`trap ... EXIT`) | always |
| 7 | ship the trained artifact before any second stage, not after | two-stage jobs |
| 8 | watch the checkpoints on a long run | over ~4 h |
| 9 | do NOT write your own GPU watcher — the platform prints the reading | always |
| 10 | ask the user for spot vs on-demand, with the numbers | always |
| 11 | ask the server for the rest (`estimate`, `validate`) | always |
| 12 | a big script or several files do not go in `args` | script > ~50 KB |
| 13 | results leave through `PACSRUN_ARTIFACT=`, never `aws s3 cp` | always |
| 14 | a second AWS account gets its own variable names | Bedrock/judge jobs |
| 15 | disk, `/dev/shm` and NCCL P2P are printed once and read after | multi-card jobs |
| 16 | pass our group coordinates to your launcher yourself | distributed jobs |

The two that cost the most:

- **Check the paths in their documentation against the repository's real layout.**
  A recipe said `runs/xxx/` and the repository had `dpo-training/runs/xxx/`.
- **Find the pairs of flags that must share one variable.** Whenever one command WRITES
  a path and a later command READS it, nothing in the shell links them, and a
  disagreement is discovered only when the later one runs. Our own instance was a LoRA
  run's `--out` and `--lora`, and the shape is general: a checkpoint directory and a
  `--resume-from`, a tokenised dataset and a `--data-dir`, an exported ONNX file and the
  server that loads it. On a 31-hour job the mismatch costs 31 hours.
- **Put a reachability check on the data, the model and the result path BEFORE the
  training command.** All three come from outside the container and all three can fail
  after a GPU has already been rented. `model_info()` confirms a model without
  downloading it; `wc -l` on the dataset catches a Git LFS pointer that cloned fine and
  contains three lines; a one-byte write to the result path catches a permission problem
  that would otherwise surface after the run. Rule 5 of the script contract has the
  four lines.

## Step 2 — never decide the GPU, the runtime or the purchase type yourself

```bash
ddpsrun estimate --name <n> --image <i> --gpu-vram 48        # any job
# and, ONLY if the job really is a TRL preference-tuning run at a sequence cap:
#   --pairs 1110 --epochs 4 --row-tokens 4100 --cap 12288
```

Report what it says, including the `confidence` and the `capacity_type` it recommends.
**`unknown` is a real answer — pass it through.**

### ★ What the estimate can and cannot answer, and this decides how you use it

**The RATE is always answerable** — it is a published price, so a cost per hour comes back
for any card, any vendor, any shape.

**The HOURS are answerable for one recipe only.** The throughput table was measured on TRL
preference tuning at two sequence caps, so `--pairs / --row-tokens / --cap` describe THAT
shape. For anything else — pretraining, SFT, an RL pipeline, a distributed run, an
inference sweep, an analytics job — the honest answer is `unknown`, and the tool gives it.

**So do not translate a job into those flags to make a number appear.** Calling a
pretraining corpus "pairs" produces a confident figure with nothing behind it, which is
the failure `unknown` exists to prevent (a prediction for an unmeasured combination was
once 96% wrong). Instead: leave them out, and ask the user for `--expected-hours`. The
estimate then multiplies THEIR hours by the real rate and labels the total
`user-supplied`, so the reader can see whose number it is.

```bash
ddpsrun estimate --name <n> --image <i> --gpu A100-80GB --gpu-count 4 \
  --vendor runpod --expected-hours 21        # cost_basis: user-supplied
```

**`capacity_type` is the user's decision and you must ask for it.** `submit` refuses without
it. on-demand costs more and is not taken away; spot is cheaper and can be reclaimed
mid-run, which on a long job with no checkpoint means losing everything. Show the estimate's
recommendation and its reason, and let them choose. It exists because a prediction was once made for a combination nobody had
measured and it was 96% wrong. Filling that gap with your own guess removes the only
protection against repeating it.

If the job IS that recipe and `estimate` wants a fact you do not have (`pairs`,
`row_tokens`, `cap`), look for it in their repository or ask them. Do not supply a
plausible number. If the job is NOT that recipe, leave those flags out entirely —
omitting them is the correct answer, not a gap to fill.

**`--region` is theirs to decide too, and leaving it out is a decision rather than a
default.** An AWS ask that names no region gets the operator's ONE default region, not a
search -- so omitting it silently picks one. It changes the price and sometimes whether
the job can run at all: the H100 is $6.88/hour in us-west-2, $8.60 in ap-northeast-1, and
in ap-northeast-2 it is sold only as an 8-GPU machine, so a one-card ask there cannot be
filled and the job sits in Pending. `ddpsrun schema` lists what is on offer.

## Step 3 — validate, and stop on an error

```bash
ddpsrun validate --name <n> --image <i> ... --script run.sh
```

**ALWAYS pass `--script`.** Most checks read the script itself and are simply
off without it — the allocator setting a script may export just before training
rather than in the job's env, whether anything leaves the container, whether a
distributed group reads its coordinates, whether there is an exit trap. A validate that never saw the
script warns about things the script already does, and the `not_checked` list is
what says so. If you built the request as a JSON file, put the script text in it.

**When this document and `--help` disagree, `--help` is right**, and
`pip install -U ddpsrun` is the fix: `../../references/cli.md` is generated from the
repository, so it can describe flags a published release does not have yet.
That happened on 2026-09-08 with `--vendor`.

**Pass `--secret` and `--vendor` here too, not just at submit.** Both changed
what validate can answer on 2026-09-08:

- a `--secret` name the deployment does not hold is now an **error** rather than
  a silent pass followed by a refusal at submit. `ddpsrun secrets` prints the
  list; a name absent from it cannot be used, and only an operator can add one.
- `--vendor runpod` stops the AWS machine-size check from judging the ask. It
  used to fire regardless, so a RunPod-only 4-card A100 job was told "AWS
  us-west-2 sells the A100 in machines of 8 cards" — true, and about a vendor
  the job had already excluded. If you see that warning now, AWS really is a
  candidate.
- naming `--capacity-type spot` with vendors that exclude AWS is an **error**:
  RunPod refuses spot before it reads any price, so no vendor is left.

Exit 1 means something would actually stop the job. Fix it and run it again. Read the
`not_checked` list aloud to the user: those are things no check could look at, so a pass
is not a guarantee.

### ★ The findings come in two tiers, and knowing which is which is your job

**PLATFORM findings are true for every job.** Can a machine of that shape be
bought; does a secret name exist and has it expired; does a group have a
rendezvous; does anything at all leave the container; is this long enough that
the credential expires first. Act on these.

**RECIPE findings are true for ONE way of training** and appear only when the
script shows that recipe — `trl-patch-missing`, `prompt-cap-too-high`,
`adapter-path-mismatch`, `gpu-too-small`. They are our own TRL preference-tuning
run's flags and its measured logits arithmetic. **If the job you are submitting
is pretraining, SFT, RL, distributed, an inference sweep or analytics, you will
not see them, and their absence is not a clean bill of health** — it means we
have not measured that shape. The `not_checked` list says so in one line; read
it out.

**Distributed jobs: pass `--group-size` and `--group-mode distributed`.**
Without them validate cannot judge the wiring, and four checks are waiting for
it: whether the script reads `PACSRUN_MASTER_ADDR` at all (an error — every rank
would wait for a rendezvous nobody hosts, with no error and no output), whether
there is a launcher, and whether `--nproc_per_node` and `--nnodes` agree with
the cards and the group size.

**One finding means "know this", not "change this".**
`aws-credential-collision` says the job carries a second AWS
identity: PACSrun injects the result-upload credentials as `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`, boto3 reads the environment
before any profile, and the upload happens at the END of the run — so getting
this wrong loses the results of a job that already cost money.
`../../references/script-contract.md` §14 has the pattern that works.

## Step 4 — SHOW THEM THE SCRIPT, then submit it with `--script`

This step used to say only "submit only after they approve", and both halves of it were
missing something.

**Show them the script itself, not a summary of it.** You wrote it from their repository
and it is about to spend their money on a rented GPU. Print the whole file and say, in
one line each, what it fetches, what it writes, and where the results go. Then ask.

**Then pass `--script run.sh` to `submit`, not just to `validate`.**

```bash
ddpsrun submit --name <n> --image <i> --script run.sh \
  --capacity-type <what they chose> ...
```

★ **`--script` IS WHAT RUNS.** With no `--arg`, the job gets
`args ['bash','-lc',<the file's text>]`, which is also the shape that makes the script
show up later under `ddpsrun` on the Scripts screen and in `GET /v1/scripts`.

**This was a real trap until 2026-09-08 and this skill walked straight into it.** Step 1
told you to write a run.sh, step 3 told you to validate it with `--script`, step 4 said
"submit" — and nothing anywhere said the script had to be sent to `submit` as well. It
was not: `script` was a validate-only field, the submit path threw it away, and the job
was created carrying no command at all. Measured on the real models: `spec.command` and
`spec.args` both absent, so the operator refuses to build the driver pod ("nothing to
run") AFTER the job has been accepted. An agent following this file wrote a script,
checked it, submitted it, and the script never ran.

**If you pass `--arg` as well, the `--arg` wins and the script is only CHECKED.** That is
deliberate -- some jobs fetch their script inside the container -- so if you pass both,
say which one is going to run.

After `ddpsrun submit` give them the `job_id` and the follow command, and offer to watch
it.

```bash
ddpsrun status <job_id>
ddpsrun logs <job_id> --follow
```

A `Recovering` phase and a non-zero restart count are **not failures**. Rented capacity
gets taken back and the job is restarted. Say so rather than reporting a problem.

## The researcher's own documents

**Code wins over prose.** When a hand-off document and the repository disagree,
read the code and say so. When two lines of the SAME document conflict — one
saying "run it with no arguments", the other asking for behaviour that needs an
argument — **do not choose. Quote both lines to the user and ask.** And before
applying a correction from a report, check IN THE CODE which task it belongs to:
on 2026-09-08 a correction meant for one pipeline (`max_tokens` 10,000) would
have been applied to another whose own value is 4096, silently changing the
experiment.

## When something goes wrong

`../../references/troubleshooting.md` has the symptoms we have actually seen, with the log
line that identifies each one.

## The reference files

| file | what | maintained by |
|---|---|---|
| `../../references/api.md` | every route and request field | generated from the server |
| `../../references/cli.md` | every command and flag | generated from `--help` |
| `../../references/script-contract.md` | how to build a run.sh that survives | written by hand |
| `../../references/troubleshooting.md` | symptoms, causes, and the log lines | written by hand |

All four are at the plugin root, so from this file they are `../../references/<name>`.
The first two are regenerated by `agent/scripts/generate_references.py` and CI fails if
they are stale. **Do not edit them.**
