#!/usr/bin/env bash
#
# HYPERUN-ENV-RENAME. A ratchet: the number of `ddpsrun` occurrences in this tree
# may go DOWN, never up.
#
# WHY A RATCHET AND NOT A BAN. The product is called hyperun. `ddpsrun` is the
# name the gateway was born with, and on 2026-09-15 it was still in 87 files and
# 675 lines. Renaming all of it in one commit is a day's work that nobody has
# scheduled, and three of those names are LIVE identifiers that cannot be changed
# by editing a file at all:
#
#   ddpsrun-gw/tokens        the Secrets Manager secret the pod reads every 60 s
#   ddpsrun/gateway          the ECR repository every image is pushed to
#   ddpsrun.io/owner         a label on 69 PacsJob objects that already exist;
#                            changing the key makes every one of them invisible
#                            to the server, which finds a job's owner by it
#
# So "ban it" would fail on day one and get switched off, which is worse than no
# check. A ratchet is the thing that actually holds: today's number is the
# ceiling, every commit has to stay under it, and the rename can then happen in
# as many small pieces as it likes.
#
# HOW TO USE IT WHEN YOU REMOVE SOME. The check will tell you the new number.
# Put that number in BASELINE below, in the same commit. Lowering it is the point.
#
# HOW TO USE IT WHEN YOU MUST ADD ONE. You almost certainly must not. The two
# legitimate reasons are (a) writing one of the three live identifiers above, and
# (b) a comment explaining the rename itself, like this one. Both are rare enough
# to be worth a conversation, which is exactly what a failing check starts.
#
# Grep anchor: HYPERUN-ENV-RENAME
set -euo pipefail

# Measured 2026-09-15 from `git ls-files`. 702 at first; deleting the dead Lambda job in
# release.yml took it to 699; the monitor put it back to 707, and every one of those eight is
# a live identifier -- the three `<oldname>.io/*` labels PACSrun writes on every job, and the
# python package's own directory name, and one more for the owner label in the analysis
# route's owner-gate test. All on the list above that a file edit cannot
# change, which is the case this file says out loud is worth a conversation.
#
# Lower it, never raise it without one. The easiest 29 to remove are in terraform/lambda/
# main.tf, whose `aws_lambda_function` and `aws_lambda_function_url` blocks now describe
# something that no longer exists -- and would rebuild it on the next apply.
# ★ 2026-09-16: 712 -> 720, AND MOST OF THE RISE WAS ALREADY ON main. Measured the same
# way on the merge-base: HEAD alone counted 718, so six of the eight arrived in commits that
# did not raise this number and CI has been failing the check on main since. The browser
# terminal adds the last two, both live identifiers this file already lists:
#   server/tests/test_terminal.py:23   `from ddpsrun_server import ...` -- the python package
#                                      directory, which a rename has to move on disk
#   server/tests/test_terminal.py:243  `ddpsrun.io/owner`, the label the controller writes on
#                                      every job and the server finds the owner by
# The new module and the WebSocket route themselves carry NONE: terminal.py is 0.
# 2026-09-16, 두 번째: 720 -> 701. terraform/lambda/ 에서 죽은 Lambda block 을 지우면서
# 19개가 같이 사라졌다. 이 파일이 "가장 쉬운 29개" 라고 지목했던 바로 그 자리다.
BASELINE=699

# ★ `git ls-files` AND NOT `grep -r .`, and the difference is the whole check.
# A recursive grep counts what is on the DISK: a virtualenv, a build directory,
# and -- on this project -- `docs/`, which is deliberately never committed
# (CLAUDE.md rule 10). Measured 2026-09-15: the same tree counted 675 through git
# and 861 through the filesystem. A ceiling that means one number on a laptop and
# another in CI is not a ceiling. Listing from git makes the two agree by
# construction, and needs no exclusion list that can go stale.
# ★ THIS FILE EXCLUDES ITSELF THROUGH GIT, NOT THROUGH grep, AND ITS OWN NAME
# CARRIES NO OLD NAME EITHER. Two mistakes on 2026-09-15, both caught by CI:
#   * the first version piped `git ls-files -z` into `grep -zv <name>`, which
#     appeared to work on a laptop and failed in CI (702 here, 709 there). The
#     exclusion was never doing anything -- the script was still UNTRACKED when
#     it was tested, so `git ls-files` did not list it. `:(exclude)` is git's own
#     pathspec and behaves the same in both places.
#   * the file was called `no-new-<oldname>.sh`, so every line that INVOKED it
#     counted. A checker whose own filename trips the check is a checker nobody
#     can satisfy.
per_file() {
  git ls-files -z -- \
      '*.py' '*.js' '*.md' '*.yaml' '*.yml' '*.toml' '*.sh' '*.tf' '*.html' '*.json' \
      ':(exclude).github/scripts/old-name-ceiling.sh' \
    | xargs -0 grep -ric 'ddpsrun' 2>/dev/null \
    | grep -v ':0$' || true
}

count() {
  per_file | awk -F: '{s += $2} END {print s + 0}'
}

NOW=$(count)

# ★ WARN ABOUT FILES GIT CANNOT SEE YET, because that is how this check got dodged twice in
# one afternoon, by the person who wrote it, both times. `git ls-files` lists the INDEX, so a
# new file counts only once it has been `git add`ed. Running the check before staging
# therefore reports the old number, passes, and lets CI find the difference minutes later.
# It cannot count an untracked file without guessing whether that file is meant to be
# committed, so it says what it did not look at instead.
UNSTAGED=$(git ls-files --others --exclude-standard -- \
    '*.py' '*.js' '*.md' '*.yaml' '*.yml' '*.toml' '*.sh' '*.tf' '*.html' '*.json' 2>/dev/null \
  | head -20)
if [ -n "$UNSTAGED" ]; then
  echo "note: these files are not in git yet and were NOT counted. Stage them and run again:"
  echo "$UNSTAGED" | sed 's/^/  /'
fi

if [ "$NOW" -gt "$BASELINE" ]; then
  echo "::error::ddpsrun occurrences went UP: $BASELINE -> $NOW (+$((NOW - BASELINE)))."
  echo ""
  echo "The product is called hyperun. Write HYPERUN_/hyperun in anything new."
  echo "If you genuinely had to write one of the live identifiers"
  echo "(ddpsrun-gw/tokens, ddpsrun/gateway, ddpsrun.io/* labels), say so in the"
  echo "pull request and raise BASELINE in .github/scripts/old-name-ceiling.sh."
  echo ""
  echo "Where they are now:"
  per_file | sort -t: -k2 -rn | head -15
  exit 1
fi

if [ "$NOW" -lt "$BASELINE" ]; then
  echo "::notice::ddpsrun occurrences went down: $BASELINE -> $NOW."
  echo "Set BASELINE=$NOW in .github/scripts/old-name-ceiling.sh so the ground held."
  # NOT an error. Failing a build for doing the right thing is how a check gets
  # deleted. The notice is in the log the author is already reading.
fi

echo "ddpsrun occurrences: $NOW (ceiling $BASELINE)"
