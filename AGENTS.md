# hyperun — a guide for AI coding agents

This repository is **the front end of PACSrun**. It gives a user who has neither kubectl nor
AWS IAM a way to submit a GPU job and get the results back.

## Start here

**Ask the tool before you read documents.** Its answers are always current.

```bash
hyperun explain          # what this tool is and how to use it
hyperun schema           # the shape of a request body
hyperun estimate ...     # time, cost and the recommended GPU. Submits nothing
hyperun validate ... --script run.sh   # points out what is wrong with this job. Submits nothing
hyperun submit -f job.yaml
hyperun status <job_id>
hyperun logs <job_id> --follow
```

**Do not judge GPU size, purchase type or expected time yourself; call `hyperun estimate`.**
This design rests on keeping exactly one copy of that judgement, on the server, and a value an
agent fills in on the spot disagrees with that copy.

**When `estimate` answers `unknown`, pass it on to the user as it is.** It is not a failure; it
is the answer. It was built that way because we once answered a number for a combination we had
never measured and were 96% out, and an agent that fills that gap with its own guess removes the
defence.

**Call `hyperun validate` before submitting, and stop if it exits 1.** The `not_checked` part of
the answer is what no check could see. Passing does not mean complete.

**One command does not exist yet:** `gpus` (the GPUs you can rent, and their prices). It needs the
server to hold vendor API keys and a catalog cache.

## When you read a user's repository to write a training script

Read `agent/references/script-contract.md`. It has nine rules and **every one of them has
actually been broken**, so each rule comes with what happened when it was. In short:

1. Check the paths in the documentation against the repository's real layout
2. Tie training's output path and inference's input path to one variable
3. Make the script produce any required output the commands do not
4. Print the file's line count right after the clone, and the sample count right after training starts
5. Use `trap ... EXIT` so that, whatever stage it dies in, everything up to then is uploaded
6. When training ends, upload the adapter first instead of waiting for inference
7. Attach a checkpoint watcher to a long training run, and kill that process when finishing
8. Print the GPU state as one `PACSRUN_GPU=` line every 30 seconds
9. Ask the server for judgements

## When something fails

`agent/references/troubleshooting.md` holds what we actually ran into and **the log lines that
identify it**. Search it by the symptom string.

## Where the four references divide

| file | what | who maintains it |
|---|---|---|
| `agent/references/api.md` | routes and request fields | **Generated.** Do not edit |
| `agent/references/cli.md` | commands and flags | **Generated.** Do not edit |
| `agent/references/script-contract.md` | how to build a run.sh | people |
| `agent/references/troubleshooting.md` | failures we hit, and their logs | people |

**Syntax is extracted from the code; pitfalls are written by people.** The first two are made by
`agent/scripts/generate_references.py` from the server's OpenAPI and the CLI's parser, and CI
checks on every push that they have not drifted from the code. Edit them by hand and that check
stops you.

## Using it from Claude Code

There is `agent/skills/hyperun/SKILL.md`. The plugin manifest is `agent/.claude-plugin/plugin.json`,
and `.claude-plugin/marketplace.json` at the top of the repository points to it. **Codex and other
agents do not read that file, so the guide everyone reads is this `AGENTS.md`.**

## Design

It is in `docs/`. Start with `00-overview.md`.
