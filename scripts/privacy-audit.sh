#!/usr/bin/env bash
# ===================================================================
#  StreamBridge :: privacy and secret audit
#
#  A public repository must not contain personal data. This script is
#  the gate before every commit: it scans tracked files for the things
#  that identify a person or a machine.
#
#  Dependency free and offline by design. No scanner to install, no
#  network access, and nothing it finds leaves the machine.
#
#  Usage:  ./scripts/privacy-audit.sh [--staged|--all-commits]
#
#    --staged       only what is about to be committed (pre-commit gate)
#    --all-commits  every blob in every commit, not just the current tree
#
#  --all-commits is the one that matters before publishing: a secret that
#  was removed in a later commit is still in the history, and rewriting
#  history is far more painful than finding it now.
# ===================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

HITS=0

red()   { printf '\033[1;31m%s\033[0m\n' "$1"; }
green() { printf '\033[1;32m%s\033[0m\n' "$1"; }
bold()  { printf '\033[1m%s\033[0m\n' "$1"; }
note()  { printf '  \033[2m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------------
#  Allowlist
#
#  Every entry is a value that is safe *because it is synthetic*: a
#  documented placeholder, a reserved range, or a value that exists in
#  the repository only to prove that redaction rejects it. Keep this
#  list short and justify each entry. Widening it is the easiest way to
#  turn this script into theatre, so every addition is a review item.
#
#  Note what is NOT allowlisted: the personal values that would be found
#  in a real leak. Those must be rewritten, not excepted.
# ---------------------------------------------------------------------

# Placeholders used in the documentation, never a real account name.
ALLOW_USER='/(home|Users)/(USER|user|someone|u)/'
# RFC 2606 / 5737 reserved names, and the example domains in test fixtures.
ALLOW_EMAIL='@(example|test|invalid|localhost|not-a-real)'
# Loopback plus the RFC 5737 documentation ranges.
# 10.0.2.2 is the Android emulator's alias for the host machine, and 10.0.0.9
# is a client address a test needs to be non-loopback. Neither is a machine.
ALLOW_IP='\b(127\.|0\.0\.0\.0|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|169\.254\.169\.254|10\.0\.0\.5|192\.168\.1\.50|10\.0\.2\.2|10\.0\.0\.9|999\.999\.999\.999)'
# The link-local cloud metadata address, which appears only as an SSRF target.
ALLOW_UUID='123e4567-e89b-12d3-a456-426614174000'
# A credential a test has to name in order to prove that a wrong one is
# refused. Obvious by construction, and asserted to be refused in the test.
ALLOW_CREDENTIAL='\b(s3cret|fake|dummy|example|test|not-the|wrong)[a-z0-9-]*["'"'"']?'
# Two things that read like private hostnames and are not. A library package
# name is not a machine, and a hostname a test asserts is *rejected* has to be
# written out in full to be a test.
ALLOW_HOSTNAME='(okhttp3\.internal|nas-\.lan)'

# This script is excluded from the scan: it necessarily contains every
# pattern it searches for.
SELF="scripts/privacy-audit.sh"

ALL_COMMITS=0
case "${1:-}" in
  --staged)
    mapfile -t FILES < <(git diff --cached --name-only --diff-filter=ACMR)
    SCOPE="staged files (${#FILES[@]})"
    ;;
  --all-commits)
    ALL_COMMITS=1
    FILES=()
    SCOPE="all commits"
    ;;
  *)
    mapfile -t FILES < <(git ls-files)
    SCOPE="tracked files (${#FILES[@]})"
    ;;
esac

# In blob mode the file list is built later, from BLOB_LIST.
if [ "$ALL_COMMITS" -eq 0 ] && [ "${#FILES[@]}" -eq 0 ]; then
  bold "privacy audit"
  note "nothing tracked yet"
  green "PASS"
  exit 0
fi

if [ "$ALL_COMMITS" -eq 1 ]; then
  # "oid path" for every object ever committed. Deduplicated by object id, so
  # a blob that has not changed since an early commit is read once rather than
  # once per commit. A tree or commit id has no path and is skipped.
  BLOB_MAP="$(mktemp)"
  trap 'rm -f "$BLOB_MAP"' EXIT
  git rev-list --objects --all \
    | awk 'NF >= 2 { print $1, $2 }' \
    | sort -u -k1,1 > "$BLOB_MAP"
  BLOBS=$(wc -l < "$BLOB_MAP")
fi

# Drop this script and any binary: a binary cannot leak text.
[ "$ALL_COMMITS" -eq 1 ] && FILES=()

if [ "$ALL_COMMITS" -eq 0 ]; then
mapfile -t FILES < <(printf '%s\n' "${FILES[@]}" | while read -r f; do
  [ -f "$f" ] || continue
  [ "$f" = "$SELF" ] && continue
  case "$(file -b --mime-encoding "$f" 2>/dev/null)" in
    binary) ;;
    *) printf '%s\n' "$f" ;;
  esac
done)
fi

bold "privacy audit - $SCOPE"
[ "$ALL_COMMITS" -eq 1 ] && note "${BLOBS} distinct object(s), content as of each commit"
echo

# scan <label> <regex> [rationale] [allow-regex] [ignore-case]
#
# Prints every match and counts it. Returns 0 when clean. A line that also
# matches allow-regex is suppressed.
#
# Case sensitivity is a flag rather than an inline (?i): GNU grep's ERE has no
# inline modifiers, and a leading (?i) is treated as a literal.
scan() {
  local label="$1" regex="$2" rationale="${3:-}" allow="${4:-}" icase="${5:-}"
  local matches count
  local -a flags=(-nE)
  [ -n "$icase" ] && flags+=(-i)

  if [ "$ALL_COMMITS" -eq 1 ]; then
    # Scan the blob contents directly rather than through "git grep <blob>":
    # given a blob instead of a tree, git grep reports a line number but no
    # path, so a finding could not be located and the allowlist regexes would
    # have nothing to anchor on. BLOB_MAP carries the path for each object.
    matches=$(
      while read -r oid path; do
        [ -n "$path" ] || continue
        git cat-file blob "$oid" 2>/dev/null \
          | grep -I "${flags[@]}" "$regex" 2>/dev/null \
          | sed "s|^|${path}:|" || true
      done < "$BLOB_MAP"
    )
  else
    # -I skips binary matches, -n prefixes file:line so a hit can be located.
    matches=$(printf '%s\n' "${FILES[@]}" \
      | xargs -r grep -I "${flags[@]}" "$regex" 2>/dev/null || true)
  fi

  if [ -n "$allow" ] && [ -n "$matches" ]; then
    matches=$(printf '%s\n' "$matches" | grep -vE "$allow" || true)
  fi
  if [ -n "$matches" ]; then
    count=$(printf '%s\n' "$matches" | wc -l)
    red "  [$label] $count hit(s)"
    printf '%s\n' "$matches" | sed 's/^/      /'
    [ -n "$rationale" ] && note "$rationale"
    HITS=$((HITS + count))
    return 1
  fi
  green "  [$label] clean"
  return 0
}

# --- identity ---------------------------------------------------------
scan "home directory" \
  '/(home|Users)/[A-Za-z0-9._-]+/' \
  "A personal home directory identifies a user. Use \$HOME, ~, or the USER placeholder." \
  "$ALLOW_USER"

scan "email address" \
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' \
  "An email address in a repository identifies a person." \
  "$ALLOW_EMAIL"

scan "IPv4 address" \
  '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' \
  "Use 127.0.0.1 for loopback. A real LAN address is machine-specific." \
  "$ALLOW_IP"

scan "UUID" \
  '\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b' \
  "Often a machine or account identifier." \
  "$ALLOW_UUID"

# --- credentials ------------------------------------------------------
scan "private key block" \
  'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY'

# A quoted literal only. An unquoted 16+ character run after `token =` is far
# more often a function name (stringPreferencesKey, tokenizeRequest) than a
# secret, and a check that cries wolf gets stopped being read.
scan "credential assignment" \
  '(api[_-]?key|secret|token|password|passwd|bearer)[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9/+_-]{16,}["'"'"']' \
  "A literal credential. Reference an environment variable instead." \
  "$ALLOW_CREDENTIAL" \
  "yes"

scan "GitHub token" \
  'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'

scan "AWS access key" \
  'AKIA[0-9A-Z]{16}'

# .gitignore lists the names it must exclude; that is prevention, not a leak.
scan "cookie or credential file" \
  '(^|/)(cookies\.txt|\.netrc|\.npmrc|\.pypirc)$' \
  "A committed credential store." \
  '^\.gitignore:'

# --- machine specifics ------------------------------------------------
# Deliberately narrow. A broader rule for .local or .home matches ordinary
# code such as config.local.toml and Path.home(), which is not a leak.
scan "private hostname" \
  '\b[a-zA-Z0-9][a-zA-Z0-9-]*\.(lan|internal|corp|intranet)\b' \
  "Looks like a machine name on a private network." \
  "$ALLOW_HOSTNAME"

# /usr and /etc are universal and safe to name. These are the locations that
# are specific to one machine. Quoted or bare: an ini ExecStart line and a
# prose sentence are both worth flagging.
scan "machine-specific absolute path" \
  '(^|[^A-Za-z0-9_-])/?(opt|srv|mnt|media)/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*' \
  "Name a generic location or use a placeholder such as <path>." \
  '^docs/.*<(opt|srv|mnt|media)/'

# --- hygiene ----------------------------------------------------------
scan "unmerged conflict marker" \
  '^(<<<<<<<|>>>>>>>)'

scan "personalised git identity" \
  '^(Author|Committer|OriginallyCommittedBy|Email):.*@[A-Za-z0-9.-]+' \
  "Present when commit metadata has been written into a file."

echo
if [ "$HITS" -gt 0 ]; then
  red "FAIL: $HITS finding(s). Reword or remove before committing."
  exit 1
fi

green "PASS: no personal data or secrets found."
exit 0
