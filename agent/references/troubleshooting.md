# Failures we hit, and the log lines that identify them

**We actually hit every one of these, and there are logs.** Each entry gives the string that
identifies the symptom first, then the cause and the remedy. This file is written by people.

---

## Training dies of OutOfMemoryError a few steps in

```
empty_strided_cuda((2, s87, 151936), (..., ...), torch.bfloat16)
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 6.75 GiB.
GPU 0 has a total capacity of 44.39 GiB of which 3.43 GiB is free.
```

**What blew up is not attention but the logits.** The three numbers in that shape are the two
answers (the good one and the bad one), the sequence length, and the vocabulary. Qwen3-4B's
vocabulary is 151,936, so **one token is 297 KiB**.

Divide the requested bytes by `2 × 151936 × 2` and you get the token count. In the case above that
is 11,926 tokens, and with a cap of 12,288, **the longest sample had grown to 97.1% of the cap.**
The mean row length was about 5,600. That is, **the cap, not the mean, decides the memory.**

There are two remedies. **Neither changes the training result.**

```bash
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python train_dpo_m3.py ...
python patch_trl_liger_slice.py $(python -c "import trl.trainer.dpo_trainer as m; print(m.__file__)")
```

**With both on, the same L40S finished the run, and the time per step also fell from 85.90 s to
66.44 s, 22.7% faster.** `hyperun validate` catches this first as `alloc-conf-missing` and
`trl-patch-missing`.

---

## `status=RUNNING` but no output for 30 minutes

```
{"id": "...", "desiredStatus": "RUNNING", "runtime": null}
```

**`status` is not a readiness signal.** We measured a container starting 42 seconds after `status`
became RUNNING. Readiness is judged by `runtime` leaving null.

Taking 30 minutes is a different problem. **When two of our jobs land on the same physical server
(RunPod's `machineId`), they share the image pull bandwidth.** Pulling an 11.04 GiB image alone ran
at 35.9–97.5 MiB/s; with two pulling at once it fell below 6.3 MiB/s, went past the 1,800-second
limit, and both died. $0.99 was wasted.

The remedy is **submitting one after another**. Do not put in jobs that use the same image at the
same time.

---

## The job is `Succeeded` but S3 is empty

**There are two causes. Look at this one first: the script did not print `PACSRUN_ARTIFACT`.**
In fetch mode that line is the only way results get out (script-contract section 13), and without
it the job ends `Succeeded` with no problem at all — the hours it burned simply vanish. Search the
driver log for `fetched`; if there is none, this is the case. On 2026-09-08 a 21-hour job stood one
line short of this.

The second cause, **until 2026-09-14, was credential expiry.** STS temporary credentials last at
most 43200 seconds (`DurationSeconds`), that is 12 hours, and a job longer than that could not
upload its own results at the end. Since PACSrun #63 and #64 (grep `PACSRUN-CREDS-FILE`) the
container's credential is a file the driver replaces before it expires, and it may write the job's
own result prefix, so this cause is gone on AWS, Shadeform and RunPod. On GCP it is not checked.

Collection by the driver is still the operator's `PACSRUN_FETCH_MODE` (`fetchMode` at
`PACSrun/internal/controller/vendorpod.go:1053`), a cluster-wide switch that cannot be turned on and
off per job, and it is on. It no longer makes the container's credential read-only -- that arm was
removed on 2026-09-14 (`PACSrun/driver/runpod/driver.py`, `_session_policy`).

**★ And an announced file that never lands is a failure now, not a silent success.** On the k3s
path (AWS, GCP, Shadeform) a job whose announced file is not in S3 ends with exit 34 rather than
`Succeeded` (`PACSRUN-K3S-FETCH`, `PACSrun/driver/common/artifact_fetch.py`), so "succeeded but
empty" becomes "failed, and says why". On AWS and GCP a file announced as the script ends can miss
that way, because the driver reads it through a container that has already stopped --
script-contract section 13 has the status and section 6 the lines that wait for it.

---

## The job ended with `exit 34`

```
the workload exited 0 but 1 of 1 announced artifact(s) never reached s3://...
```

**Training succeeded and its results could not get out.** This is why it is not exit 20 — the
program did its job, so blaming the researcher's code points the wrong way. It is in the 30–39
band, so the operator solves again and the vendor is blamed (capped by `maxFetchFailures`).

One of these three shows in the driver log.

| log | meaning | remedy |
|---|---|---|
| `stat said ... No such file or directory` | there is no file at the announced path | a typo in the path, or it was printed before the file was made |
| `arrived short: N of M bytes` | the read was cut off. **The partial object was deleted** | usually the machine was reclaimed. Run it again |
| `GIVING UP on ... after 3 attempts` | all three attempts failed | the two lines above record the reason before it |

**The most common case is announcing a file that is still being written.** Print after the `tar`
has closed (script-contract section 13).

---

## `AccessDenied ... CreateMultipartUpload` shows up

```
An error occurred (AccessDenied) when calling the CreateMultipartUpload operation
```

**Until 2026-09-14 this was normal**: in fetch mode the remote got only a read-only credential and
the driver did the uploading. That arm was removed (PACSRUN-CREDS-FILE), and the container's
credential may now write the job's own result prefix. So this line now means one of three things:

| where | why |
|---|---|
| in the first seconds of the run | the driver has not written the credentials file yet -- wait for it (script-contract section 17) |
| a key outside `$PACSRUN_RESULT_PATH` | the credential writes the job's own prefix and nothing else |
| a GCP job | whether the GCP driver writes the file is not checked |

---

## `no offering left` though the vendor has stock

```
stopped after 5 offering(s) refused
```

**A misclassification fixed on 2026-08-28.** Every HTTP 400 in RunPod's create response was being
read as out of stock. The real cause was this.

```
Field "objectMounts" is not defined by type "PodFindAndDeployOnDemandInput"
```

The vendor had changed its API schema and our request was refused; it had nothing to do with stock.
The way to check is **to try `gpuCount` at 99.** If the same error comes back, the request is
refused before the stock check, so it is a schema problem.

Now a malformed request, a quota and a stock shortage are told apart, and each ends with a
different exit code.

---

## Training ends as soon as it starts with `표본 탈락: ... 재확인 필요`

(The trainer prints this in Korean: "samples dropped: ... needs rechecking".)

It means the data has a prompt longer than `--max-prompt-len`. Check the cap pair. The ones we used
are `12288 / 11264` and `18432 / 17408`, and both leave 1,024 tokens for the answer.

`hyperun validate` catches this as `prompt-cap-too-high`.

---

## Training finished but inference cannot find the adapter

```
OSError: /root/ab/adapter_bank does not appear to have a file named adapter_config.json
```

Training's `--out` and inference's `--lora` differ. **It shows only after the whole training run,
which makes it the most expensive mistake.** Rule 2 of `script-contract.md` prevents it, and
`hyperun validate --script run.sh` catches it in advance as `adapter-path-mismatch`.

---

## The model download dies at once

This happens when `HF_HUB_ENABLE_HF_TRANSFER=1` is set but the `hf_transfer` package is missing.
Set it to `0`, or install the package.

---

## vllm inference raises a tokenizer error

```
AttributeError: 'Qwen2Tokenizer' object has no attribute 'all_special_tokens_extended'
```

This is when `transformers` 5.15 or later is installed. Check the pin `"transformers<5"`.
