#!/bin/bash
# Verify MP4, MOV and M4A import on this Mac, and write one plain report to send back.
#
#   bash scripts/verify-macos-import.sh
#
# Run it in Terminal from a checkout of the branch under test. Plan on about an hour, mostly unattended; keep the
# Mac awake and plugged in, and close other heavy apps. When it ends it prints where the report is:
#   .tmp/macos-verify/report.txt
# Read it, then send that one file back. It holds versions and results only: no user name, home folder, host name,
# serial number or recording. Other files in .tmp/macos-verify/ (logs, generated media) stay on this Mac.
#
# What it does, for Python 3.11 and then 3.12:
#   1. Records the environment: macOS version, chip, uv, Python, PyAV and FFmpeg versions, the commit.
#   2. Sets up with `uv sync --frozen` and the same extras as Modwen.command (stt, desktop, and mac on Apple
#      Silicon), plus the dev group for pytest. If that fails, the failure is the result for that Python: it is
#      recorded with uv's own last lines and nothing is worked around.
#   3. Runs the import, isolation, safety and preflight tests (pytest), listing any test skipped here.
#   4. Runs a real engine on 127.0.0.1 (fake speech-to-text, fake local LLM): hostile MP4/MOV files made here,
#      a real memory-limit breach with its peak footprint and overshoot, the CPU limit, a frozen child, a
#      CPU-spinning child, every core busy, an engine kill with restart and cleanup, imports at once, 7.1 audio,
#      and valid fragmented and edit-list media.
# Every line in the report is PASS, FAIL or NOT RUN. The report is evidence for the team, not an approval.
#
# What it never does: download a model, use a recording, or use the network for anything but dependency setup
# (uv, Python and packages). Nothing outside this checkout changes: Python, packages, caches and temp files live
# in .tmp/macos-verify/; if uv isn't installed, the pinned, checksum-verified uv the desktop app uses is fetched
# into .cache/desktop-uv/. To clean up afterwards: rm -rf .tmp/macos-verify
#
# For the team's dry runs only (the report lists any that were used, and such a run is partial):
#   MODWEN_VERIFY_WORK=dir             work folder instead of .tmp/macos-verify
#   MODWEN_VERIFY_PYTHONS="3.12"       which Pythons (default "3.11 3.12")
#   MODWEN_VERIFY_PYTHON_3_11=path     use this interpreter and skip setup (also _3_12)
#   MODWEN_VERIFY_PYTEST_ARGS="-k x"   extra pytest arguments
#   MODWEN_VERIFY_CHECKS=a,b           only these engine checks (names in scripts/verify_macos_import.py)
#   MODWEN_VERIFY_ALLOW_NON_MAC=1      run off macOS (a harness dry run; never a macOS result)

cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd -P)
UV_PIN=0.12.24   # the version desktop/scripts/stage-engine.sh bundles

OS=$(uname -s)
if [ "$OS" != "Darwin" ] && [ -z "${MODWEN_VERIFY_ALLOW_NON_MAC:-}" ]; then
  echo "This script verifies macOS. On $OS it would test nothing that matters here, so it stops."
  exit 2
fi

WORK=${MODWEN_VERIFY_WORK:-$ROOT/.tmp/macos-verify}
mkdir -p "$WORK" || exit 1
WORK=$(cd "$WORK" && pwd -P)
REPORT=$WORK/report.txt
BODY=$WORK/report-body.txt
: >"$BODY"

# Everything this run writes stays in the checkout. Nothing from a shell profile steers Python or Modwen.
export UV_CACHE_DIR=$WORK/uv-cache UV_PYTHON_INSTALL_DIR=$WORK/python UV_PYTHON_BIN_DIR=$WORK/python-bin
export UV_PYTHON_PREFERENCE=only-managed UV_NO_CONFIG=1 UV_TOOL_DIR=$WORK/uv-tools
export TMPDIR=$WORK/tmp PYTHONPYCACHEPREFIX=$WORK/pycache XDG_CACHE_HOME=$WORK/xdg-cache
export HF_HOME=$WORK/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
mkdir -p "$TMPDIR"
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX UV_PROJECT_ENVIRONMENT UV_PYTHON
for name in $(env | sed -n 's/^\(MODWEN_[A-Za-z0-9_]*\)=.*/\1/p'); do
  case $name in MODWEN_VERIFY_*) ;; *) unset "$name" ;; esac
done

say() { echo "$*"; echo "$*" >>"$BODY"; }
result() {  # status check detail
  printf '%-8s %-7s %-30s %s\n' "$1" "-" "$2" "$3" | tee -a "$BODY"
}

# Replace anything that could identify this person or Mac. Literal strings, case-insensitive.
esc() { printf '%s' "$1" | sed -e 's/[]\/$*.^|[]/\\&/g'; }
sanitize() {
  local host user
  host=$(hostname 2>/dev/null | cut -d. -f1)
  user=$(id -un 2>/dev/null)
  sed -e "s|$(esc "$ROOT")|<checkout>|g" -e "s|$(esc "$HOME")|~|g" "$1" >"$1.tmp" && mv "$1.tmp" "$1"
  for word in "$user" "$host"; do
    if [ ${#word} -ge 3 ]; then
      LC_ALL=C sed -e "s|$(esc "$word")|<private>|gI" "$1" >"$1.tmp" 2>/dev/null && mv "$1.tmp" "$1" ||
        { sed -e "s|$(esc "$word")|<private>|g" "$1" >"$1.tmp" && mv "$1.tmp" "$1"; }
    fi
  done
}

STARTED=$(date -u +%Y-%m-%dT%H:%MZ)
BEGAN=$(date +%s)
say "== Environment"
if [ "$OS" = "Darwin" ]; then
  say "  macOS            $(sw_vers -productVersion 2>/dev/null) ($(sw_vers -buildVersion 2>/dev/null))"
  say "  chip             $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
  say "  architecture     $(uname -m)$([ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ] && echo ' (this shell runs under Rosetta)')"
  say "  cores / memory   $(sysctl -n hw.ncpu 2>/dev/null) logical / $(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 )) GiB"
else
  say "  NOT macOS        $OS $(uname -m): a dry run of the harness, not a macOS result"
fi
say "  commit           $(git rev-parse HEAD 2>/dev/null || echo unknown) ($(git rev-parse --abbrev-ref HEAD 2>/dev/null))"
say "  local changes    $(git status --porcelain 2>/dev/null | grep -v '^??' | wc -l | tr -d ' ') changed tracked files"

# uv: the one on PATH, else the desktop app's pinned build, checksum-verified, kept inside the checkout.
export PATH="$HOME/.local/bin:$PATH"
UV=$(command -v uv 2>/dev/null)
UV_FROM="found on PATH"
if [ -z "$UV" ] && [ "$OS" = "Darwin" ]; then
  case "$(uname -m)" in arm64) TRIPLE=aarch64-apple-darwin ;; *) TRIPLE=x86_64-apple-darwin ;; esac
  CACHE=$ROOT/.cache/desktop-uv/$UV_PIN
  ARCHIVE=uv-$TRIPLE.tar.gz
  if [ ! -x "$CACHE/uv" ]; then
    mkdir -p "$CACHE"
    BASE=https://github.com/astral-sh/uv/releases/download/$UV_PIN
    { curl -fsSL "$BASE/$ARCHIVE" -o "$CACHE/$ARCHIVE" && curl -fsSL "$BASE/$ARCHIVE.sha256" -o "$CACHE/$ARCHIVE.sha256" &&
      (cd "$CACHE" && shasum -a 256 -c "$ARCHIVE.sha256") && tar -xzf "$CACHE/$ARCHIVE" -C "$CACHE" --strip-components 1
    } >"$WORK/uv-download.log" 2>&1
  fi
  [ -x "$CACHE/uv" ] && UV=$CACHE/uv && UV_FROM="pinned $UV_PIN, downloaded and checksum-verified"
fi
if [ -n "$UV" ]; then say "  uv               $("$UV" --version 2>/dev/null | cut -d' ' -f2) ($UV_FROM)"
else say "  uv               not available (not on PATH; download of $UV_PIN failed or not attempted)"; fi

OVERRIDES=$(env | grep '^MODWEN_VERIFY_' | sed 's/=.*//' | tr '\n' ' ')
[ -n "$OVERRIDES" ] && say "  overrides        $OVERRIDES(a partial or dry run)"

EXTRAS="--extra stt --extra desktop"
if [ "$(uname -m)" = "arm64" ]; then EXTRAS="$EXTRAS --extra mac"; fi
PYTHONS=${MODWEN_VERIFY_PYTHONS:-"3.11 3.12"}
CHECKS=${MODWEN_VERIFY_CHECKS:-}

for v in $PYTHONS; do
  say ""
  say "== Python $v"
  var=MODWEN_VERIFY_PYTHON_${v/./_}
  given=${!var:-}
  venv=$WORK/venv-$v
  PY=
  if [ -n "$given" ]; then
    PY=$given
    result "NOT RUN" "setup.$v" "uv sync not run: interpreter supplied by $var (dry run)"
  elif [ -z "$UV" ]; then
    result "FAIL" "setup.$v" "no uv to set up with; see uv above"
  else
    echo "Setting up Python $v (uv sync --frozen --python $v $EXTRAS); the first time downloads packages..."
    UV_PROJECT_ENVIRONMENT=$venv "$UV" sync --frozen --python "$v" $EXTRAS >"$WORK/setup-$v.log" 2>&1
    code=$?
    if [ $code -ne 0 ]; then
      result "FAIL" "setup.$v" "uv sync --frozen --python $v $EXTRAS exited $code; uv's last lines:"
      tail -n 25 "$WORK/setup-$v.log" | sed 's/^/         | /' >>"$BODY"
    else
      PY=$venv/bin/python
      [ -x "$PY" ] || PY=$venv/Scripts/python.exe
      got=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
      if [ "$got" = "$v" ]; then
        result "PASS" "setup.$v" "uv sync --frozen --python $v $EXTRAS (with the dev group for pytest)"
      else
        result "FAIL" "setup.$v" "the environment runs Python '$got', not $v"
        PY=
      fi
    fi
  fi
  if [ -z "$PY" ]; then
    result "NOT RUN" "tests.$v" "setup failed, so no test or engine check ran on Python $v"
    continue
  fi
  "$PY" scripts/verify_macos_import.py env --py "$v" --out "$BODY" ||
    result "FAIL" "env.$v" "this Python can't import PyAV, numpy, soxr or psutil"
  echo "Python $v: import tests (pytest)..."
  # shellcheck disable=SC2086
  "$PY" scripts/verify_macos_import.py pytest --py "$v" --work "$WORK" --out "$BODY" -- ${MODWEN_VERIFY_PYTEST_ARGS:-}
  echo "Python $v: real engine checks..."
  "$PY" scripts/verify_macos_import.py engine --py "$v" --work "$WORK" --out "$BODY" --only "$CHECKS"
done

PASSES=$(grep -c '^PASS ' "$BODY")
FAILS=$(grep -c '^FAIL ' "$BODY")
NOTRUN=$(grep -c '^NOT RUN ' "$BODY")
{
  echo "Modwen: macOS import verification report"
  echo "Generated by scripts/verify-macos-import.sh, started $STARTED, took $(( ($(date +%s) - BEGAN) / 60 )) min."
  echo "One run on one Mac. It is evidence for the team to read, not an approval of a macOS release."
  echo
  echo "Results: $PASSES PASS, $FAILS FAIL, $NOTRUN NOT RUN (one line per check below)."
  echo
  cat "$BODY"
  echo
  echo "== Not covered by this script (NOT RUN here)"
  echo "  The packaged app and DMG, WKWebView, the native file picker, real speech-to-text and LLM models,"
  echo "  system-audio capture, a disconnected Mac or packet capture, and any Mac other than this one."
} >"$REPORT"
sanitize "$REPORT"
rm -f "$BODY"

echo
echo "Done: $PASSES PASS, $FAILS FAIL, $NOTRUN NOT RUN."
echo "Send back this one file: ${REPORT#"$ROOT"/}"
[ "$FAILS" -eq 0 ]
