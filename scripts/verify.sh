#!/usr/bin/env bash
# ===================================================================
#  StreamBridge :: full verification
#
#  Runs everything a change can break, in the order that fails fastest:
#  static checks, then the offline test suite, then a privacy audit.
#  Nothing here touches the network except the explicitly optional live
#  step at the end.
#
#  Usage:
#    ./scripts/verify.sh
#    ./scripts/verify.sh --live    # also the networked playback check
# ===================================================================
set -uo pipefail

cd "$(dirname "$0")/.."

LIVE=0
FAILURES=0

for arg in "$@"; do
    case "$arg" in
        --live) LIVE=1 ;;
        -h|--help) sed -n '3,13p' "$0" | sed 's/^#  \{0,1\}//'; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

bold()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()    { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
skip()  { printf '  \033[2mskip\033[0m %s\n' "$1"; }

# Run a step, record the result, keep going. One failing step must not
# hide the state of the others.
step() {
    local label="$1"; shift
    if "$@" >/tmp/streambridge-verify.log 2>&1; then
        ok "$label"
    else
        fail "$label"
        sed 's/^/       /' /tmp/streambridge-verify.log | tail -25
    fi
}

PY="${PYTHON:-python3}"

# ------------------------------------------------------------------
bold "Environment"
# ------------------------------------------------------------------
if "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    ok "python >= 3.11"
else
    fail "python >= 3.11"
    printf '\nCannot continue without a supported interpreter.\n'
    exit 1
fi

# Prefer the project venv, which is where the dependencies are.
if [ -x .venv/bin/python ]; then
    PY=".venv/bin/python"
    ok "using .venv/bin/python"
fi

for module in pytest; do
    "$PY" -c "import $module" 2>/dev/null \
        && ok "$module available" \
        || { fail "$module missing (pip install -e '.[dev]')"; }
done

# ------------------------------------------------------------------
bold "Static analysis"
# ------------------------------------------------------------------
if "$PY" -c 'import ruff' 2>/dev/null || command -v ruff >/dev/null 2>&1; then
    step "ruff check"    "${RUFF:-ruff}" check .
    step "ruff format --check" "${RUFF:-ruff}" format --check .
else
    skip "ruff not installed"
fi
if "$PY" -c 'import mypy' 2>/dev/null || command -v mypy >/dev/null 2>&1; then
    step "mypy" "${MYPY:-mypy}"
else
    skip "mypy not installed"
fi

# ------------------------------------------------------------------
bold "Tests"
# ------------------------------------------------------------------
# The suite is offline and deterministic: every external process is faked.
step "pytest (offline)" "$PY" -m pytest -q

# ------------------------------------------------------------------
bold "Privacy"
# ------------------------------------------------------------------
step "privacy audit" ./scripts/privacy-audit.sh

# ------------------------------------------------------------------
bold "Packaging"
# ------------------------------------------------------------------
step "build metadata" "$PY" -c "
import tomllib, pathlib
data = tomllib.loads(pathlib.Path('pyproject.toml').read_text())
name = data['project']['name']
assert name == 'streambridge', name
scripts = data['project']['scripts']
for required in ('streambridge', 'streambridge-server'):
    assert required in scripts, required
"

# ------------------------------------------------------------------
bold "Web assets"
# ------------------------------------------------------------------
step "assets are complete" "$PY" -c "
import pathlib
web = pathlib.Path('src/streambridge/web')
for name in ('index.html', 'app.css', 'app.js', 'manifest.webmanifest'):
    assert (web / name).is_file(), name
"

# ------------------------------------------------------------------
if [ "$LIVE" -eq 1 ]; then
    bold "Live (networked)"
    if [ -f scripts/e2e-test.sh ]; then
        step "end-to-end playback" ./scripts/e2e-test.sh
    else
        skip "scripts/e2e-test.sh not present"
    fi
else
    skip "live playback check (--live to include it)"
fi

# ------------------------------------------------------------------
printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf '\033[1;32mVERIFY PASSED\033[0m\n'
    exit 0
fi
printf '\033[1;31mVERIFY FAILED: %d step(s)\033[0m\n' "$FAILURES"
exit 1
