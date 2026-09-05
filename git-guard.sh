#!/bin/bash
#
# Pre-flight scan. Sourced by git-push-current.sh and git-commit.sh.
#
# This repo is PUBLIC. Anything committed here is world-readable the moment it is
# pushed, and a force-push does not remove it — old objects stay fetchable by SHA.
# So the check runs before the push, not after.
#
# Two layers:
#   1. Generic patterns below — credentials, keys, private hosts, IPs, emails.
#   2. .git-deny-patterns (gitignored, optional) — one regex per line, for names
#      that must never appear here but that would themselves be a leak if they
#      were hardcoded in a public script. Create it locally; it never ships.
#
# NOTE: this scan is never truncated. A partial scan reported as an all-clear is
# how credentials reached this repo's history in the first place.

set -uo pipefail

GUARD_GENERIC=(
  '(password|passwd|secret|api[_-]?key|access[_-]?token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{6,}'
  '(http_basic_auth|basic_auth|htpasswd)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']+["'"'"']'
  '["'"'"'][A-Za-z0-9]{6,}:[A-Za-z0-9]{6,}["'"'"']'   # a quoted user:pass pair
  'AIza[0-9A-Za-z_-]{20,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'https?://[^/[:space:]]+:[^@/[:space:]]+@'          # user:pass@host
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'    # email addresses
  '\b[a-z0-9-]+\.local\b'                             # private dev hostnames
  '\b(25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(\.(25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}\b'  # IPv4
)

# Lines that legitimately contain a trigger word. Keep this list short and specific.
GUARD_ALLOW='example\.(com|local|org)|your-site|mysite\.local|user@example|placeholder|<[A-Z_]+>|make\.wordpress\.org|@param|@return'

guard_scan() {
  local subject="$1" content="$2" found=0 pat line

  local hits=""
  for pat in "${GUARD_GENERIC[@]}"; do
    line=$(printf '%s\n' "$content" | grep -inE -e "$pat" 2>/dev/null | grep -vE "$GUARD_ALLOW")
    [[ -n "$line" ]] && hits+="$line"$'\n'
  done
  if [[ -n "${hits// /}" ]]; then
    printf '\n  ✖ %s — possible sensitive content:\n' "$subject"
    printf '%s' "$hits" | sort -u -t: -k1,1n | sed 's/^/      /'
    found=1
  fi

  if [[ -f .git-deny-patterns ]]; then
    local denied=""
    while IFS= read -r pat; do
      [[ -z "$pat" || "$pat" == \#* ]] && continue
      line=$(printf '%s\n' "$content" | grep -inE -e "$pat" 2>/dev/null)
      [[ -n "$line" ]] && denied+="$line"$'\n'
    done < .git-deny-patterns
    if [[ -n "${denied// /}" ]]; then
      printf '\n  ✖ %s — matches a local deny pattern:\n' "$subject"
      printf '%s' "$denied" | sort -u -t: -k1,1n | sed 's/^/      /'
      found=1
    fi
  fi

  return $found
}

guard_check() {
  local rc=0

  echo "Pre-flight scan (repo is public)…"

  # Everything about to be pushed: the diff against the remote, or the whole tree
  # if the branch has no upstream yet.
  local branch diff
  branch=$(git branch --show-current)
  if git rev-parse --verify --quiet "origin/$branch" >/dev/null; then
    diff=$(git diff "origin/$branch..HEAD" 2>/dev/null)
  else
    diff=$(git grep -I --no-color -n '' HEAD 2>/dev/null)
  fi
  guard_scan "outgoing changes" "$diff" || rc=1

  # Commit messages travel too, and are easy to forget.
  local msgs
  if git rev-parse --verify --quiet "origin/$branch" >/dev/null; then
    msgs=$(git log --format='%s%n%b' "origin/$branch..HEAD" 2>/dev/null)
  else
    msgs=$(git log --format='%s%n%b' 2>/dev/null)
  fi
  guard_scan "commit messages" "$msgs" || rc=1

  # Config files must never be tracked, whatever .gitignore says today.
  local tracked
  tracked=$(git ls-files | grep -E '^configs/.+\.json$' || true)
  if [[ -n "$tracked" ]]; then
    printf '\n  ✖ site config files are tracked — these hold credentials:\n'
    printf '%s\n' "$tracked" | sed 's/^/      /'
    rc=1
  fi

  if [[ $rc -ne 0 ]]; then
    cat <<'MSG'

  ─────────────────────────────────────────────────────────────────────
  Push stopped. Review each line above.

  If a hit is a false positive, add a narrow exception to GUARD_ALLOW in
  git-guard.sh. Do not disable the scan.

  If it is real: remove it, and treat the value as compromised — rotate
  it. Rewriting history later does not unpublish anything.
  ─────────────────────────────────────────────────────────────────────
MSG
    return 1
  fi

  echo "  ✔ clean — no credentials, private hosts or denied terms found"
  return 0
}
