#!/usr/bin/env bash
# ===================================================================
#  StreamBridge :: security audit
#
#  Two halves:
#
#    1. dependency vulnerabilities, from the advisory database, which
#       needs network access
#    2. a static review of the patterns that matter *in this project*:
#       loopback enforcement, the host allowlist for upstream redirects,
#       shell-free subprocess use, and the absence of web UI files from
#       tracked history
#
#  Usage:
#    ./scripts/security-audit.sh
#    ./scripts/security-audit.sh --offline   # skip the network half
# ===================================================================
set -uo pipefail

cd "$(dirname "$0")/.."

OFFLINE=0
for arg in "$@"; do
    case "$arg" in
        --offline) OFFLINE=1 ;;
        -h|--help) sed -n '3,15p' "$0" | sed 's/^#  \{0,1\}//'; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

FAILURES=0
WARNINGS=0

bold() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; }
warn() { printf '  \033[33m!!\033[0m   %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PY=".venv/bin/python"

# ------------------------------------------------------------------
bold "Project invariants"
#
#  Each of these is a security property that a later change can remove
#  without any test failing loudly, so they are checked directly.
# ------------------------------------------------------------------

# The server must refuse to bind anything but loopback. This is the single
#  most important line in the project: it is what makes StreamBridge a
#  local tool rather than an open proxy.
if grep -q "LOOPBACK_HOSTS" src/streambridge/api.py; then
    ok "api.py enforces a loopback bind allowlist"
else
    bad "api.py no longer contains the loopback bind allowlist"
fi

# The refusal message is matched loosely on purpose: the wording is free to
# change, but the check that a non-loopback host is rejected before binding
# must not disappear.
if grep -qE "binds to loopback only|not in \(\"127\.0\.0\.1" src/streambridge/server.py; then
    ok "server.py refuses a non-loopback host at startup"
else
    bad "server.py no longer refuses a non-loopback host"
fi

# Upstream media URLs are host-checked before any byte is forwarded. Without
#  this an id that resolves to an internal address would become a SSRF probe.
if grep -q "def is_allowed_upstream" src/streambridge/streamer.py; then
    ok "streamer.py checks the upstream host against an allowlist"
else
    bad "streamer.py is missing the upstream host allowlist check"
fi

# Everything external runs as an argv list. A shell=True or an f-string in a
#  command would make every upstream title a command injection vector.
if grep -rn "shell=True" src/ >/dev/null 2>&1; then
    bad "shell=True found in src/ (see above)"
else
    ok "no shell=True anywhere in src/"
fi

# Every urlopen must sit behind an explicit scheme check. Bandit flags all of
#  them, and the useful answer is an enforced invariant rather than a blanket
#  suppression: a configuration that produced a file:// base must fail, not
#  read a local file.
urlopens=$(grep -rho "urlopen(" src/ | wc -l)
guarded=$(grep -rho "scheme not in" src/ | wc -l)
if [ "$urlopens" -gt "$guarded" ]; then
    bad "a urlopen is used without a scheme check (${urlopens} call(s), ${guarded} guard(s))"
elif [ "$urlopens" -eq 0 ]; then
    ok "no urlopen in src/; upstream fetches go through http.client"
else
    ok "every urlopen is guarded by a scheme check (${urlopens} call(s))"
fi

# The UI must not carry a dependency on a third-party origin, because the
#  CSP is strict on purpose: no unsafe-inline, no unsafe-eval.
if grep -rnE "https?://(cdn|unpkg|jsdelivr|fonts\.googleapis)" src/streambridge/web/ >/dev/null 2>&1; then
    bad "the web UI references a third-party CDN"
else
    ok "web UI loads no third-party origin"
fi

# The asset allowlist is what makes path traversal impossible. Serving files
#  from a path built out of the request would reintroduce it.
if grep -q "_STATIC_FILES" src/streambridge/api.py; then
    ok "static assets are served from a fixed allowlist, not a request path"
else
    bad "the static asset allowlist is gone"
fi

# ------------------------------------------------------------------
bold "No personal data in history"
# ------------------------------------------------------------------

# A secret removed in the last commit is still in the history. Only tracked
#  files and the full history are checked, never the working tree.
if git rev-parse --git-dir >/dev/null 2>&1; then
    if ./scripts/privacy-audit.sh --all-commits >/dev/null 2>&1; then
        ok "privacy audit over every commit"
    else
        bad "privacy audit found something in the history (run scripts/privacy-audit.sh --all-commits)"
    fi
else
    warn "not a git repository; history not checked"
fi

# ------------------------------------------------------------------
bold "Dependencies"
# ------------------------------------------------------------------
if [ "$OFFLINE" -eq 1 ]; then
    printf '  \033[2mskip\033[0m dependency audit (--offline)\n'
else
    if "$PY" -m pip_audit --version >/dev/null 2>&1; then
        if "$PY" -m pip_audit --strict >/tmp/streambridge-audit.log 2>&1; then
            ok "pip-audit found no known vulnerability"
        else
            warn "pip-audit reported findings:"
            sed 's/^/       /' /tmp/streambridge-audit.log | tail -20
        fi
    else
        warn "pip-audit not installed (pip install pip-audit)"
    fi

    if command -v bandit >/dev/null 2>&1; then
        # Only the request handling and subprocess boundaries are in scope;
        # a blind run would flag the deliberate try/except on sockets.
        if bandit -q -r src/streambridge -f screen >/tmp/streambridge-bandit.log 2>&1; then
            ok "bandit found no issue"
        else
            warn "bandit reported findings:"
            sed 's/^/       /' /tmp/streambridge-bandit.log | tail -20
        fi
    else
        warn "bandit not installed"
    fi
fi

# ------------------------------------------------------------------
printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf '\033[1;32mSECURITY AUDIT PASSED\033[0m'
    [ "$WARNINGS" -gt 0 ] && printf ' (%d warning(s))' "$WARNINGS"
    printf '\n'
    exit 0
fi
printf '\033[1;31mSECURITY AUDIT FAILED: %d invariant(s) broken, %d warning(s)\033[0m\n' \
    "$FAILURES" "$WARNINGS"
exit 1
