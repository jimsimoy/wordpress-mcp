#!/bin/bash
#
# Push the current branch, but only after the pre-flight scan passes.
# This repo is public — see git-guard.sh for why the check runs first.

set -uo pipefail
cd "$(dirname "$0")" || exit 1
source ./git-guard.sh

guard_check || exit 1

BRANCH=$(git branch --show-current)
echo
echo "Pushing ${BRANCH} → origin…"
git push origin "$BRANCH"
