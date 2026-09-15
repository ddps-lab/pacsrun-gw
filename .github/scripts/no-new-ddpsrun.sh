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

# Measured 2026-09-15 from `git ls-files`, right after the Lambda was deleted.
# Lower it, never raise it. The easiest 29 to remove are in terraform/lambda/main.tf,
# whose `aws_lambda_function` and `aws_lambda_function_url` blocks now describe
# something that no longer exists -- and would rebuild it on the next apply.
BASELINE=702

# ★ `git ls-files` AND NOT `grep -r .`, and the difference is the whole check.
# A recursive grep counts what is on the DISK: a virtualenv, a build directory,
# and -- on this project -- `docs/`, which is deliberately never committed
# (CLAUDE.md rule 10). Measured 2026-09-15: the same tree counted 675 through git
# and 861 through the filesystem. A ceiling that means one number on a laptop and
# another in CI is not a ceiling. Listing from git makes the two agree by
# construction, and needs no exclusion list that can go stale.
per_file() {
  git ls-files -z -- \
      '*.py' '*.js' '*.md' '*.yaml' '*.yml' '*.toml' '*.sh' '*.tf' '*.html' '*.json' \
    | grep -zv 'no-new-ddpsrun\.sh' \
    | xargs -0 grep -ric 'ddpsrun' 2>/dev/null \
    | grep -v ':0$' || true
}

count() {
  per_file | awk -F: '{s += $2} END {print s + 0}'
}

NOW=$(count)

if [ "$NOW" -gt "$BASELINE" ]; then
  echo "::error::ddpsrun occurrences went UP: $BASELINE -> $NOW (+$((NOW - BASELINE)))."
  echo ""
  echo "The product is called hyperun. Write HYPERUN_/hyperun in anything new."
  echo "If you genuinely had to write one of the live identifiers"
  echo "(ddpsrun-gw/tokens, ddpsrun/gateway, ddpsrun.io/* labels), say so in the"
  echo "pull request and raise BASELINE in .github/scripts/no-new-ddpsrun.sh."
  echo ""
  echo "Where they are now:"
  per_file | sort -t: -k2 -rn | head -15
  exit 1
fi

if [ "$NOW" -lt "$BASELINE" ]; then
  echo "::notice::ddpsrun occurrences went down: $BASELINE -> $NOW."
  echo "Set BASELINE=$NOW in .github/scripts/no-new-ddpsrun.sh so the ground held."
  # NOT an error. Failing a build for doing the right thing is how a check gets
  # deleted. The notice is in the log the author is already reading.
fi

echo "ddpsrun occurrences: $NOW (ceiling $BASELINE)"
