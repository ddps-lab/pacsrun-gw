# What to keep to when you build a run.sh

**Every one of these is something we filled in by hand across 8 experiments.** Each rule records
what actually happened when it was broken. There is no rule here without evidence.

This file is written by people. It is not generated.

---

## 1. Check the repository's real layout against its documentation

The paths in the documentation a researcher hands over can differ from the repository's real
layout.

**What actually happened.** The documentation said `runs/gradedpairs_20260826/` and the repository
had `dpo-training/runs/gradedpairs_20260826/`. It would have died right after the clone.

```bash
# Check right after the clone. Better than finding out 25 hours later
git clone --depth 1 "$REPO" src
ls src/dpo-training/runs/*/pairs/ | head
wc -l src/.../pairs/train_exp2_bank.jsonl
```

---

## 2. Find the pairs that must share one variable

**When one command writes a path and a later command reads it, nothing in the shell connects the
two.** If they disagree, the first command still succeeds, and the mismatch shows only when the
later one runs. The pairs come in many shapes — a checkpoint directory and `--resume-from`, a
tokenized dataset and `--data-dir`, an exported ONNX file and the server that reads it, and the
LoRA adapter below. **Make one variable and use it on both sides.**

Below is an example from our job (TRL preference tuning + inference).

```bash
# Not this
python train_dpo_m3.py --out adapter_bank_v2
python gen_openrca_tasks_fast.py --lora /root/ab/adapter_bank      # differs

# This
ADAPTER="adapter_${JOB}"
python train_dpo_m3.py --out "$ADAPTER"
python gen_openrca_tasks_fast.py --lora "/root/ab/$ADAPTER"
```

**When they disagree, the first part finishes and only then does the second fail.** For AIOps,
training alone is 31 hours. `hyperun validate --script run.sh` catches it as
`adapter-path-mismatch` **only when the two flags are named `--out`/`--lora`** (recipe tier,
DDPSRUN-CHECK-TIERS). A pair under other names is invisible to the check — which is why this rule
comes before the check.

---

## 3. Make the script produce the outputs no command produces

Of the five files the researcher was to get back, **the training command did not produce three.**

| file | who makes it |
|---|---|
| `adapter_<job>/` | the training command |
| `out_<job>.jsonl` | the inference command |
| `train_<job>.log` | **the script has to make it with `tee`** |
| `score_<job>.txt` | **the script has to make it with `tee`** |
| `pipfreeze_<job>.txt` | **the script has to make it with `pip freeze`** |

```bash
python train_dpo_m3.py ... 2>&1 | tee "train_${JOB}.log"
python score_openrca_corrected.py ... 2>&1 | tee "score_${JOB}.txt"
"$VENV_TRAIN/bin/pip" freeze > "pipfreeze_${JOB}.txt"
```

---

## 4. Put the intermediate checks early

```bash
# right after the clone
echo "pair file line count: $(wc -l < "$PAIRS")"
# right after training starts, check the two lines the trainer prints (the trainer prints them
# in Korean; the English is a gloss)
#   학습 쌍 N개                                           N training pairs
#   [검증] 트레이너 최종 학습 표본 N / 투입 N              [check] trainer's final samples N / fed N
#                                                         <- the two must match for zero dropped
```

**Better than finding out after 25 hours of running that the file was wrong.**

---

## 5. Check that the dataset and the model are reachable before training

**Both have to come from outside the container, and both can fail silently.** But they fail at
different moments.

```
training pairs   missing right after the clone, it dies at once.  Cheap
model            the download takes minutes, and it dies after that
                 the GPU is already rented by then, so it is expensive
```

**So put two checking lines in front of the training command.**

```bash
# data
test -s "$PAIRS" || { echo "pair file is missing or empty: $PAIRS"; exit 1; }
echo "pair file line count: $(wc -l < "$PAIRS")"

# model.  There is no knowing whether it is reachable until you ask
python - <<'PY'
import os
from huggingface_hub import model_info
name = os.environ["BASE_MODEL"]
info = model_info(name)                       # dies here if it does not exist or you lack access
print(f"model checked: {name}, {len(info.siblings)} files")
PY
```

- `model_info` **lists the files without downloading the weights.** It takes seconds.
- Once these two lines pass, the hours of training after them will not die for want of data or a
  model.

**There are three ways it can be unreachable, each with a different cause.**

| symptom | cause |
|---|---|
| `401` or `403` | a gated model, so a token is needed. Put `HF_TOKEN` in `secrets` |
| `404` | the name is wrong. The organisation part has to be exact too |
| no connection at all | the remote cannot reach the internet. A vendor configuration problem |

**Watch for data in the repository that is managed by Git LFS.** The clone succeeds, but the
file's content is a single pointer. Printing the line count shows this at once.

```bash
# An LFS pointer looks like this.  Suspect it when the line count is 3
version https://git-lfs.github.com/spec/v1
oid sha256:...
size 12345678
```

**Check the place results go to as well.** Finding out you have no permission after training ends
is too late.

```bash
echo probe | aws s3 cp - "$PACSRUN_RESULT_PATH.probe" \
  && aws s3 rm "$PACSRUN_RESULT_PATH.probe" \
  || { echo "cannot write to the result path: $PACSRUN_RESULT_PATH"; exit 1; }
```

`hyperun validate` cannot look at these for you. **Neither the user's repository nor the vendor's
credentials are visible from the server.** So they have to be in the script.

---

## 6. Whatever stage it dies in, upload everything up to then

```bash
upload_everything() {
  # make the file -> announce it. Announcing is §13's contract, and the driver collects it.
  cp "train_${JOB}.log" /root/work/ && echo "PACSRUN_ARTIFACT=/root/work/train_${JOB}.log"
  if [ -d "$ADAPTER" ]; then
    tar czf /root/work/adapter.tar.gz "$ADAPTER" \
      && echo "PACSRUN_ARTIFACT=/root/work/adapter.tar.gz"
  fi
  # A second safety net needed only on AWS/GCP until the k3s fetch is deployed (the ★ in §13).
  aws s3 cp "train_${JOB}.log" "$RESULT_PATH" || true
  [ -f /root/work/adapter.tar.gz ] \
    && aws s3 cp /root/work/adapter.tar.gz "$RESULT_PATH$ADAPTER.tar.gz" || true
}
trap upload_everything EXIT
```

`trap ... EXIT` runs on a normal exit, on an error, and on SIGTERM. Without it, a job that dies in
its 20th hour has **spent all the money and left nothing.** `hyperun validate` catches this as
`no-exit-trap`.

**★ Why announcing inside the trap is safe.** After the workload ends, the driver keeps the
machine and waits up to 600 seconds for its queue to empty (§13). So even a line printed at the
last moment is collected — unlike `aws s3 cp`, an announce does not depend on a credential that
expires.

---

## 7. Export an earlier stage's output first, instead of waiting for the later stage

```bash
python train_dpo_m3.py ... | tee "train_${JOB}.log"
tar czf /root/work/adapter.tar.gz "$ADAPTER"                       # export it here first
echo "PACSRUN_ARTIFACT=/root/work/adapter.tar.gz"
python gen_openrca_tasks_fast.py ...                                 # then inference
```

**Every job where a short stage follows a long one has this shape** — inference after training,
evaluation after pretraining, export after training. At 25 hours + 1 hour, **dying in the last
hour must not lose the first 25.** An announce tells the driver "take this now", and the fetch
runs while the later stage does.

### ★ A folder left out for its size: look inside it once more, file by file

Excluding large outputs from staging is right. If training writes an adapter every round and it
is hundreds of MB, it cannot be exported every 15 minutes. **The mistake comes next: has anyone
looked at whether small files needed for a verdict or a resume live in the folder that was
excluded whole?**

On 2026-09-15 we nearly lost one that way. Training writes two things to the same folder every
round.

```
runs/adapters/<agent>/iter_<k>/
    adapter_model.safetensors     147 MB   ← excluded from staging for its size
    ppo_stats.jsonl                 2 KB   ← went out with it
```

`ppo_stats.jsonl` gets one line of kl and entropy per step, and it was **the only continue/stop
criterion that job's submission sheet set**. From the moment the folder was excluded, that file
went nowhere until one dataset had finished all three rounds and the adapter bundle went out.
Losing the machine in between would have left nothing to decide on.

**So divide by file, not by folder.**

```bash
# Not this — excluding adapters whole takes the small files in it out too
ls -d runs/iter_* runs/C

# This — leave out only the large ones, and name the small ones used for the verdict
ls -d runs/iter_* runs/C runs/adapters/*/iter_*/ppo_stats.jsonl
```

**The repository does not tell you which file that is.** Where the training script writes is
learned by reading its code, and that a file is the basis of the verdict is learned by reading the
submission sheet or the handover document. Setting the two side by side is the whole of this
rule: **take each file the submission sheet says "decide by this value" about, and confirm that
its path is actually in the staging list.**

---

## 7b. A script whose output path is not fixed — collection follows the real folder

Sometimes the training script a wrapper calls has its output path **hard-coded**: `runs/` written
literally in twenty-six places in the file, with no argument and no environment variable. To run
such code in several branches without editing it, **swap what that name points to** from the
outside.

```bash
for ds in $DATASETS; do
  mkdir -p out/$ds; rm -f runs; ln -sfn out/$ds runs     # move the label over
  bash their_train.sh                                     # does not know where it writes
  rm -f runs                                              # and take it off
done
```

**Do not look at that label when you write the collection.** `runs/` exists only while one branch
runs; it is gone between branches and after the job ends. A tar by that name comes up empty at
every boundary, and the final bundle holds nothing. **Look at the real folder (`out/`) directly**
— it keeps growing whether the label is there or not.

On 2026-09-15 we submitted a wrapper fixed this way. The version before the fix would have made an
empty tar at the end.

---

## 8. Attach a checkpoint watcher to long training

The trainer writes `checkpoint-NNN/` locally every epoch. To move it to S3, compress it **after it
has been fully written**.

```bash
watch_checkpoints() {
  while true; do
    sleep 60
    for dir in "$ADAPTER"/checkpoint-*; do
      [ -d "$dir" ] || continue
      [ -f "$dir/.uploaded" ] && continue
      # only touch what has not changed for 120 s. A tar taken mid-write uploads half
      [ -n "$(find "$dir" -newermt '-120 seconds' -print -quit)" ] && continue
      # close the tar first, then announce. The order is the rule -- §13's size check turns a
      # file still being written into a failed fetch (it is not uploaded truncated; it is not
      # uploaded).
      tar czf "/root/work/$(basename "$dir").tar.gz" "$dir" \
        && echo "PACSRUN_ARTIFACT=/root/work/$(basename "$dir").tar.gz" \
        && touch "$dir/.uploaded"
    done
  done
}
watch_checkpoints & WATCH_PID=$!
```

**Always kill the watcher on exit.** If it stays alive, `tee` never gets EOF, so `PACSRUN_EXIT=`
is never printed and the driver never learns the job has ended.

```bash
# ★ Do not default it to 0. `kill 0` is not PID 0 but **the whole process group**, and if the
# trap fires before the watcher starts (dying in a check above), the script kills itself.
# Adding `${WATCH_PID:-0}` with `set -u` in mind is a natural reflex, and at that moment it
# turns into a silent suicide. Check for empty first. On 2026-09-09 one session made this
# itself and caught it.
on_exit() {
  [ -n "${WATCH_PID:-}" ] && kill "$WATCH_PID" 2>/dev/null || true
  upload_everything
}
trap on_exit EXIT
```

---

## 9. The script no longer has to print the GPU state

**This section is no longer something to do.** PACSrun's driver puts `driver/common/gpu-watch.sh`
in front of the workload's command and prints it itself. It does so on every vendor — the ones
that give a VM (AWS, GCP) use the file as the k3s pod's command, and the one that gives a
container (RunPod) as a wrapper. The grep anchor is `PACSRUN-GPU-WATCH`.

That only one stream, stdout, comes out of the remote container is unchanged. What changed is who
writes into that stream, and **the metrics appear even if the researcher forgets** — that is all
this change is.

The printed line is the same.

```
PACSRUN_GPU=94,38200,45440,71,298
```

- The format is `utilization,memory_used,memory_total,temperature,power`. The server reads them in
  this order.
- One line every 30 seconds is 3,000 lines for a 25-hour job. Against a training log of 350,000
  lines, that is negligible.
- **An old script that still has `watch_gpu` does not break.** The same line is printed twice every
  30 seconds, and the server uses the last one. Delete it if you like, or leave it.
- If the metrics do not show, look in the log for lines starting with `PACSRUN_GPU_WATCH`. That line
  says whether the watcher started, or whether it skipped because the image has no `nvidia-smi`.
- These five values are everything `nvidia-smi` gives, and **they are not "how much the card
  worked".** `utilization.gpu` is defined as "the fraction of time **at least one** kernel was
  running", so using one of an H100's 132 SMs reads as 100%. The value that answers that question
  is DCGM's profiling field, which needs `CAP_SYS_ADMIN`, and RunPod's create request has no
  capability field at all.

---

## 10. Ask the user how the machine is bought

**The server does not decide `--capacity-type`; the person submitting does.** Without it, the
submit is refused.

```bash
hyperun estimate ...          # gives a recommendation and the reason
hyperun submit ... --capacity-type on-demand
```

- `on-demand` costs more and is not taken away.
- `spot` is cheaper and can be reclaimed mid-run. **A long training run without checkpoints loses
  everything.**
- RunPod does not sell spot, so a `spot` submit drops RunPod from the candidates.
- Shadeform has no spot prices in the catalogue either. So with `spot`, the only vendor that can be
  priced is AWS. The vendors that can run a job are aws, runpod and shadeform, and
  `hyperun estimate` prices all three (since 2026-09-28).

**Do not choose on the user's behalf.** Show the recommendation and the reason, and let the user
answer.

## 11. Ask the server for the other judgements

Write GPU size, purchase type and expected time **neither in the script nor in the skill.**

```bash
hyperun estimate --gpu-vram 48 --pairs 1110 --epochs 4 --row-tokens 4100 --cap 12288
```

That way the logic lives in one place, and the UI, the CLI and the agent all get the same answer.
Write a number here, and while the server accumulates new measurements this file alone keeps
giving the old answer.

---

## 12b. ★ The script body becomes that process's command line — `pkill -f` matches itself

This is exactly what section 12 says, followed through. Because `spec.args = ["bash", "-lc",
<body>]`, the script starts on the remote **carrying its own full text as its command line**.
`pkill -f` / `pgrep -f` / `ps | grep` match against **the full command line**, so if the name of
what you mean to kill appears anywhere in the body, **the script matches itself.** Comments are
part of the body.

**2026-09-11.** A script that called `pkill -f nccl_test.py` after an NCCL pre-test died of
SIGTERM 14 seconds in (exit 143 = 128+15), because the line just above it had
`torch.distributed.run ... nccl_test.py`.

**2026-09-14.** A training script calls `pkill -9 -f "[v]llm"` in its GPU cleanup loop, and **three
comment lines** of the wrapper around it had the same lower-case word. With `-9` the trap does not
run either. It would have hit after logging and scoring, just before training — about 2.6 hours on
four A100s, about $17. It was found and removed before submission.

**A cleverer pattern does not help.** As long as the target's name is in the body, it matches. Do
one of two things.

```bash
# (a) exclude yourself and your ancestors by PID -- when we write the wrapper
ANC=" $$ ${BASHPID:-$$} "
for pid in $(pgrep -f "$PATTERN"); do
  case "$ANC" in *" $pid "*) continue;; esac
  kill "$pid"
done

# (b) when a script we cannot change calls pkill, remove that word from our own body
#     what to remove is known only by reading that script's pkill lines
grep -n "pkill\|pgrep" "$THEIR_SCRIPT"        # always, before submitting
```

**Pre-submit check.** If a script the wrapper calls has `pkill -f`, grep your own body for each of
its patterns. If the count is not 0, fix it.

---

## 12. When the script grows or is several files — do not put it into args as it is

The body sent with `--script run.sh` **goes inside the job object.** `to_pacsjob` loads it as
`spec.args = ["bash", "-lc", <body>]`, and that object is stored in etcd. So there is a ceiling:
`script` is up to **256 KiB** (`models.SCRIPT_MAX_CHARS`), and past it the submit is refused with
422. It costs nothing, since no GPU has been rented yet, but it means this is not the place for
something large.

**Measured (2026-09-08, the cluster's `baseline-c`).** The training script itself is small — at
around 20 KiB it would be 8% of the ceiling put in as it is. But that job was already using
another method:

```
whole PacsJob object   4,682 bytes
spec.args                302 bytes      <- the bootstrap below
run.sh in S3          19,655 bytes      <- the actual training script
```

The 302 bytes in `spec.args` are all of it.

```bash
set -euo pipefail
pip install --quiet --no-input boto3
python3 - <<'PY2'
import os, urllib.parse, boto3
u = urllib.parse.urlparse(os.environ["PACSRUN_RESULT_PATH"])
base = u.path.lstrip("/").rstrip("/")
boto3.client("s3").download_file(u.netloc, f"{base}/run.sh", "/root/run.sh")
PY2
bash /root/run.sh
```

**The two methods, and when to use which.**

| method | when | cost |
|---|---|---|
| `--script run.sh` (the body in args) | one file, under 256 KiB. **Use this by default** | none. The job contains what it ran, so `hyperun`'s Scripts screen, the Submitted spec and resubmission all work |
| S3 bootstrap (the 302 bytes above) | the script exceeds the ceiling, is several files, or someone wants to swap the script without resubmitting the job | one more point of failure. It can die unable to read S3 **after the GPU is already rented**, so add that object to rule 5's reachability check. And the job object alone does not show what ran |
| `git clone` (rule 1) | the code is in a repository | as above. The clone has to come before the training command |

**If you settle on S3, ask the user to upload.** The agent does not upload that object itself —
`hyperun` has no upload command, and the result prefix is one the server creates per job, so its
address does not even exist before the submit. The order is: the user uploads with
`aws s3 cp run.sh <path>`, tells the agent that path, and the agent sends the bootstrap above with
`--script`.

---

## 13. Export results with `PACSRUN_ARTIFACT` — write them with `aws s3 cp` and you lose them after 21 hours

**We nearly lost results for lack of this section.** On 2026-09-08 a session about to submit task C
with only the repository **could not find this contract anywhere in the documentation**, and
learned it by chance from an old wrapper that had been committed to the repository along with the
result tar of a 09-04 job. Without that chance it would have written with `aws s3 cp`, and 21 hours
later every result would have vanished with `AccessDenied`. The `troubleshooting.md` entry "The job
is `Succeeded` but S3 is empty" is the trace of that failure.

### ★ That one line travels on the log — when the log stops, nothing is uploaded

The contract holds on one condition. **The driver reads that line from the remote's log.** If the
vendor's log channel breaks, an announce does nothing, and the script has no way to know — it
keeps printing fine.

**2026-09-14.** One vendor's log query began answering with a refusal for whole pods (68 times over
63 minutes on one job, 21 times on another). That vendor's current API specification has **no log
path at all** — none of its 23 paths is for logs. That day a job ran normally for 8 hours and
**ended with 0 results**, and even the line announcing its end did not get through, so the machine
was not handed back.

**So look at the result path once after the first announce.** It is the same idea as rule 5's
"check the pipe first"; only the thing checked differs.

```bash
echo "PACSRUN_ARTIFACT=$FIRST_SMALL_FILE"
sleep 120
# Is that name visible in the result path? If not, the announce is not getting through.
aws s3 ls "$PACSRUN_RESULT_PATH" | grep -q "$(basename "$FIRST_SMALL_FILE")" \
  || echo "★ the announce is not getting through -- suspect the log channel, and tell a person"
```

**What can be done when it does not get through.** Nothing on the script's side can fix it. But
**leaving that state in the log** lets a person collect the results another way. That vendor
already runs an HTTP server that serves artifacts by absolute path, and on 2026-09-14 everything
was recovered through it. **Ending without a word is the worst outcome.**

### The contract

**Right after you finish** a file, print one line to stdout. The driver then collects that file.

```bash
tar czf /root/work/adapter.tar.gz "$ADAPTER"
echo "PACSRUN_ARTIFACT=/root/work/adapter.tar.gz"
```

- **The path is an absolute path inside the container.** The driver reads that path and moves the
  file out.
- **One line per file.** Several files, several lines, in any order.
- **Print it after the file is complete.** Announce a file that is still being written and a
  truncated file is collected. For a `tar`, after it has closed; for a log, after its last flush.

### The file name is the S3 name — two files with the same name overwrite each other

The key is **the job's result prefix + the file name (basename)**. `/root/work/adapter.tar.gz` goes
to `s3://<bucket>/<prefix>adapter.tar.gz`. The front of the path is discarded, so announcing both
`runs/iter_1/ckpt.pt` and `runs/iter_2/ckpt.pt` means **the later one overwrites the earlier.** Put
the round or the rank into the file name: `ckpt_iter2.pt`, `adapter_rank0.tar.gz`.

### Why not `aws s3 cp`

On **every vendor, the driver** is what writes results to S3. Two reasons.

- **The credential given to the container expires first.** AWS's ceiling is 43,200 seconds (12
  hours), so a 21-hour job's **last** upload — the reason for the run — happens after it expires.
- **The upload method differs by vendor.** If the script had to know it, the script would be tied
  to a vendor. An announce line is the same sentence everywhere.

That does not make the credential the container receives useless. **It reads its own prefix with
it** — getting back the previous round's checkpoint when continuing to the next round is what it
is for (the last part of §13, `continue_from`).

Only the way the driver fetches files differs by vendor, and **the script does not need to know
the difference.**

| vendor | how the driver fetches |
|---|---|
| RunPod | a GET to a small HTTP server inside the container via `<pod-id>-8888.proxy.runpod.net` (`PACSrun/driver/runpod/driver.py:233` `ARTIFACT_RE`, `:2012` `_fetch_one`) |
| VM + k3s (AWS, GCP, Shadeform. Seeweb later) | the size with `stat -c %s` and the bytes with `cat`, both through the k3s API's exec (`PACSrun/driver/common/artifact_fetch.py`, grep `PACSRUN-K3S-FETCH`) |

**★ Status as of 2026-09-09: the k3s path is implemented and not yet deployed.** So **on AWS/GCP,
keep `aws s3 cp` alongside the announce for now** — then it survives on either path (on RunPod
`aws s3 cp` fails silently with AccessDenied and the announce does the work). Once it is deployed,
an announce alone is enough, and `aws s3 cp` is the part that depends on the credential that
expires after 12 hours, so it is better removed. **Which state it is in, `hyperun explain`
answers** — ask the server, not this document.

#### Two checks the k3s path makes, which the script needs to know about

- **It compares sizes.** If the byte count `stat` gave differs from what actually arrived, it
  **deletes the object** and tries again (3 times). So **announcing a file that is still being
  written makes the fetch fail** — the file is not uploaded truncated; it is not uploaded at all.
  That is why the rule above says to print after completion.
- **If the exit is 0 but something announced is not in S3, the job ends with exit 34.** Training
  succeeded but its results did not get out, so it is judged not a success — better than an empty
  prefix marked Succeeded.

`explain` saying only "Write it there yourself" is a sentence from before this section existed.

### Check once, first, that the pipe works (this replaces rule 5's result path check)

Finding out after burning 21 hours of training that nothing can be collected is too late. **Try it
first with one small file.**

```bash
date > /root/work/_probe.txt
echo "PACSRUN_ARTIFACT=/root/work/_probe.txt"
```

Look for that line coming back in the driver log as `fetched ... bytes`, then start training.

**In the log, that line shows as `<internal>=/root/work/_probe.txt`.** The gateway's log relay
masks names that start with `PACSRUN_` (`redact` in `server/ddpsrun_server/k8s.py`;
`server/tests/test_k8s.py:23` pins that behaviour), and **the path stays, so the check still
works.** The name not showing is not a failure — the line not being there at all is.

### Exported round by round, it survives an interruption

After `Recovering`, the container **starts again empty** — `training.resumable` is a claim the user
makes, and the tool gives nothing back for it. Export each round's output with the contract above
as the round ends, and even if the machine is reclaimed in the 15th hour, the rounds up to then
remain.

**A fact you can rely on:** the **result path stays the same** after a restart. The server makes it
once from the job id and puts it in `spec.resultPath`, and recovery uses the same PacsJob, so that
field does not change.

---

## 14. A job that uses a second AWS account separates the three variables itself

**PACSrun injects the credential for collecting results as `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`.** When a job calls a **different** AWS account —
a Bedrock judge, for example — that code looks for the same three names. **boto3 reads environment
variables before profiles, so `AWS_PROFILE` does not separate them.**

**Whichever side wins, the other half breaks.** If the judge loses, evaluation is refused; if
collection loses, **the results of a 21-hour run cannot be uploaded at the end** — collection
happens when the job ends, so that failure shows at the most expensive moment.

`validate` says this first: if the job holds one of the three names directly, or another name with
both `AWS` and `ACCESS_KEY` in it is present, `aws-credential-collision` fires
(`check_aws_credential_collision` in `server/ddpsrun_server/validate.py`).

**The shape that works — receive the judge key under its own name, and pass it explicitly only
where it is used.**

```bash
python - <<'PY'
import os, boto3
s = boto3.session.Session(
    aws_access_key_id=os.environ["JUDGE_AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["JUDGE_AWS_SECRET_ACCESS_KEY"],
    aws_session_token=os.environ.get("JUDGE_AWS_SESSION_TOKEN"),
)
bedrock = s.client("bedrock-runtime", region_name="us-west-2")
PY
```

**Only when the researcher's code uses boto3's default chain and cannot be changed**, swap the
three variables to the judge's values before that part runs and **restore them when it ends.**
Without the restore, rule 13's collection breaks.

```bash
# swap -- only ever as a pair with the restore.
export _SAVED_KEY="$AWS_ACCESS_KEY_ID" _SAVED_SECRET="$AWS_SECRET_ACCESS_KEY" _SAVED_TOKEN="$AWS_SESSION_TOKEN"
export AWS_ACCESS_KEY_ID="$JUDGE_AWS_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$JUDGE_AWS_SECRET_ACCESS_KEY" AWS_SESSION_TOKEN="$JUDGE_AWS_SESSION_TOKEN"
python evaluate.py            # the part that calls the judge
export AWS_ACCESS_KEY_ID="$_SAVED_KEY" AWS_SECRET_ACCESS_KEY="$_SAVED_SECRET" AWS_SESSION_TOKEN="$_SAVED_TOKEN"
```

---

## 15. Three things known only after the machine is up — the script prints one line for each

These three **cannot be set at submit time.** The schema has no field for them, and the values
depend on which host you got. So the rule is not "request it" but "**check it and record it**".

| what | where the value comes from now | what the script does |
|---|---|---|
| disk | the operator-wide `PACSRUN_DISK_GB=200`. It cannot be set per job | print one line of `df -h /root` before training. Measured on 09-04: 3 venvs + a model around 30 GB fit in 200 GB |
| `/dev/shm` | set by the host you got. No field | print `df -h /dev/shm`. TP4 vLLM needs it, so if it is small, leave that fact in the log and tell the user the tensor parallel size should come down |
| NCCL P2P | on some RunPod hosts the first all-reduce hangs. No field | set `NCCL_P2P_DISABLE` **in one place** so it can be reverted. The platform catches the hang itself and ends it with exit 21 |

```bash
# before training, in order. The point is that all three lines stay in the log.
df -h /root /dev/shm
nvidia-smi --query-gpu=index,name,memory.total --format=csv
: "${NCCL_P2P_DISABLE:=0}"; export NCCL_P2P_DISABLE
echo "NCCL_P2P_DISABLE=$NCCL_P2P_DISABLE"
```

---

## 16. Distributed training — we give the coordinates, the script passes them to the launcher

**If the pods have to talk to each other, submit with `--group-size N --group-mode distributed`.**
Without it, N pods are **N independent runs that do not know each other.** They do finish, they
cost N machines, and they leave N unrelated results.

### What we give, by name

| variable | what | who fills it |
|---|---|---|
| `PACSRUN_GROUP_SIZE` | how many pods this group has | the operator (`PACSRUN-GROUP-COORDS`) |
| `PACSRUN_GROUP_RANK` | which one this pod is within its group | the operator |
| `PACSRUN_GROUP_INDEX` | which group of the job this group is | the operator |
| `PACSRUN_MASTER_ADDR` | the **private** address of the rank 0 machine | the driver (`PACSRUN-GROUP-HOSTNET`) |
| `PACSRUN_MASTER_PORT` | `29500 + group_index` | the driver |
| `PACSRUN_POD_INDEX` | which one this is among all the job's pods. It stays separate from the group | the operator |

**That the names belong to no framework is intentional.** torchrun wants `--node_rank`/`--master_addr`,
and other launchers want other things. **That translation is the line the script writes.**

```bash
torchrun \
  --nnodes "$PACSRUN_GROUP_SIZE" \
  --node_rank "$PACSRUN_GROUP_RANK" \
  --master_addr "$PACSRUN_MASTER_ADDR" \
  --master_port "$PACSRUN_MASTER_PORT" \
  --nproc_per_node 4 \
  train.py
```

### ★ If the coordinates are not read, it stops without a sound

`driver/common/remotek8s.py` records the measurement: **"NEITHER RANK PRINTED ANYTHING. Both
sat in `dist.init_process_group` with no error and no output."** Every rank waits for a rendezvous
nobody opened, **the card reads as busy** (NCCL's wait is a running kernel), and it is billed until
the stall detector ends it with exit 21 after 3,600 seconds.

`hyperun validate --group-size N --group-mode distributed --script run.sh` looks at four things:
reading none of the coordinates is an **error** (`group-coords-unread`), no launcher is a warning,
`--nproc_per_node` different from `--gpu-count` is an error, and `--nnodes` different from the
group size is an error.

### Do not write a value in two places

Hard-code `--nnodes 2` and it silently disagrees the day you change to `--group-size 4`. **Use
`$PACSRUN_GROUP_SIZE`.** `--nproc_per_node` is the number of cards per pod, so it has to equal
`--gpu-count`, and validate compares the two.

### Keep performance expectations low

**One pod boundary is slower than a single card.** Even within the same machine it was 0.56×
(`facts/pod-boundary-costs-ddp.md`). What it takes away is not P2P but one thing, **shared
memory**. Two machines are 3.07× slower again. And crossing regions adds a 60–70 ms round trip
and $0.02 per GB — there is a measurement of one step moving 866,890,752 bytes (2026-09-05).
**In other words, distribution is not done "to go faster" but "because it does not fit on one
card".**

### `/dev/shm` and NCCL P2P are known only after the machine is up

That is the table in section 15. In particular, **on some RunPod hosts the first all-reduce hangs**
— set that value in one place so it can be reverted with `NCCL_P2P_DISABLE=1`.
