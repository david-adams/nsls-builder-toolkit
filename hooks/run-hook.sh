#!/usr/bin/env bash
# run-hook.sh — the single cross-platform entry point for the toolkit's Python
# hooks.
#
# WHY THIS EXISTS. hooks.json has no OS conditional, so a plugin hook that runs
# Python has to name one interpreter for every machine. The two alternatives
# both fail somewhere:
#   * a bare `python3` entry errors on stock Windows, where `python3` is a
#     Microsoft Store alias that exits without running anything (that error is
#     why the gate was pulled out of hooks.json on 2026-09-06, which is what
#     left every migrated Mac with no gate at all);
#   * registering the hook twice (`python3` and `py -3`) means the absent twin
#     prints a "<hook> hook error" notice on EVERY matching tool call, on every
#     machine — unsuppressable, because swallowing it needs a shell.
# One shell-form entry per hook, with "shell": "bash" named explicitly in
# hooks.json, resolves the interpreter here instead. install.ps1 hard-requires
# Git for Windows, so Git Bash is present wherever this has to run.
#
# CONTRACT: exit 2 passes through (the only code that blocks a tool call);
# everything else exits 0 with whatever the script printed. A hook that cannot
# run is silence, never a notice — fail open is the gate's first design rule.
#
# The same launcher runs the helpers Claude itself calls from the guardrail
# policy (memory, event, similar, logit), with their arguments passed through. Those
# used to be written as `python3 ...`, which does not exist on stock Windows, so
# on a PC a builder's "no thanks" was never recorded and the event never sent.
set -u

sub="${1:-}"
[ "$#" -gt 0 ] && shift
root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
  root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)" || exit 0
fi

case "$sub" in
  gate)          script="$root/hooks/guardrail-gate.py" ;;
  session-start) script="$root/hooks/session-start.py" ;;
  policy)        script="$root/hooks/guardrail-policy.py" ;;
  memory)        script="$root/hooks/guardrail-memory.py" ;;
  event)         script="$root/hooks/guardrail-event.py" ;;
  similar)       script="$root/hooks/similar-skills.py" ;;
  checkpoint)    script="$root/hooks/logit-checkpoint.py" ;;
  logit)         script="$root/hooks/log-it.py" ;;
  # "Update my NSLS toolkit" (skills/update-toolkit): session-start.py's own
  # update code, run on request with time to finish.
  update)        script="$root/hooks/session-start.py"; set -- --update-now ;;
  *)             exit 0 ;;
esac
[ -f "$script" ] || exit 0

# Resolving the interpreter costs a probe subprocess, and this runs before every
# Bash/Write/Edit call, so the answer is cached per machine. The cache holds a
# format tag and an ABSOLUTE path: validating it is then a file test, a changed
# format invalidates every old entry at once, and a Python that was upgraded or
# removed fails the test and costs exactly one re-probe.
# Every fallback is itself defaulted. With `set -u`, expanding $HOME when HOME
# is unset aborts the script outright — before the interpreter is even probed —
# so the gate does not fail open, it fails silent, which is the one outcome
# this file exists to prevent. Same hole the Python side had at import time.
cache_dir="${CLAUDE_CONFIG_DIR:-${HOME:-${TMPDIR:-/tmp}}/.claude}"
cache="$cache_dir/.nsls-hook-python"
# v3: one field per LINE. v2 packed the three fields with "|", which truncates
# any interpreter path containing that character and then silently runs nothing.
# A newline cannot occur in a path either shell can hand back, so there is no
# escaping to get wrong. The tag change invalidates every v2 entry at once.
CACHE_FORMAT=v3
py=""
flag=""
if [ -r "$cache" ]; then
  { IFS= read -r c_fmt; IFS= read -r c_py; IFS= read -r c_flag; } < "$cache" 2>/dev/null || true
  if [ "${c_fmt:-}" = "$CACHE_FORMAT" ] && [ -n "${c_py:-}" ] && [ -x "${c_py}" ]; then
    py="$c_py"
    flag="${c_flag:-}"
  fi
fi

# A zero exit status is NOT proof of a Python. install.ps1 documents exactly what
# this guards against: the stock Windows `python` / `python3` App Execution
# Aliases can exit 0 having run nothing at all. A probe that accepted exit 0
# would cache one of those, and from then on every hook would "succeed" having
# executed nothing -- silently, no notice, no beacon, no gate. That is this
# entire outage reproduced one layer down. So the probe demands a sentinel on
# stdout, which only a real Python 3 can produce.
# A candidate that hangs would otherwise hold the whole probe loop until the
# hook's own 10s timeout, so the interpreters after it are never reached. Cap
# each probe WHERE THE TOOL EXISTS: `timeout` ships with Git for Windows and on
# Linux, and is absent from a stock macOS. Hard-coding it would make every probe
# on a Mac fail "command not found" — no interpreter found, gate silently off,
# which is the outage this file exists to end. Unquoted on purpose below: empty
# must expand to no argument at all.
_probe_cap=""
if command -v timeout >/dev/null 2>&1; then
  _probe_cap="timeout 5"
fi

probe() {
  _out=""
  if [ -n "${2:-}" ]; then
    _out=$($_probe_cap "$1" "$2" -c 'import sys; sys.stdout.write("NSLS-PY-%d" % sys.version_info[0])' 2>/dev/null)
  else
    _out=$($_probe_cap "$1" -c 'import sys; sys.stdout.write("NSLS-PY-%d" % sys.version_info[0])' 2>/dev/null)
  fi
  [ "$_out" = "NSLS-PY-3" ]
}

# Every match on PATH, in order, not just the first. A broken stub earlier in
# PATH must not be able to hide a working interpreter behind it -- on Windows
# the Store aliases sit in exactly that position. Newline-separated, and read
# without word splitting, because a real Windows Python path contains spaces.
# Answers through globals rather than stdout. Printing "path|flag" and splitting
# it back apart meant an interpreter path containing "|" lost everything after
# it, and the launcher then ran a path that does not exist — silently, because
# this file fails open by design. There is no serialisation left to break.
PICKED_PY=""
PICKED_FLAG=""
pick_python() {
  local spec cand cand_flag resolved candidates
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    cand="${spec%%|*}"
    cand_flag="${spec#*|}"
    case "$cand" in
      /*|[A-Za-z]:[\\/]*) candidates="$cand" ;;
      *) candidates="$(type -aP "$cand" 2>/dev/null)" ;;
    esac
    [ -n "$candidates" ] || continue
    while IFS= read -r resolved; do
      [ -n "$resolved" ] || continue
      [ -x "$resolved" ] || continue
      if probe "$resolved" "$cand_flag"; then
        PICKED_PY="$resolved"
        PICKED_FLAG="$cand_flag"
        return 0
      fi
    done <<CANDIDATES
$candidates
CANDIDATES
    # Last resort for a bare name: let the shell resolve it itself. `type -aP`
    # answers one path per line, so an interpreter whose path contains a
    # newline comes back as fragments, none of which are executable, and the
    # candidate is skipped -- the gate then fails silent, which is the one
    # outcome this file exists to prevent. Invoking the name directly
    # serialises nothing. Additive: it only runs when every resolved path has
    # already been rejected.
    case "$cand" in
      /*|[A-Za-z]:[\\/]*) ;;
      *)
        if probe "$cand" "$cand_flag"; then
          PICKED_PY="$cand"
          PICKED_FLAG="$cand_flag"
          return 0
        fi
        ;;
    esac
  done <<ORDER
$order
ORDER
  return 1
}

if [ -z "$py" ]; then
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*|Windows*)
      # `py` first: the real launcher, never an alias. Then the explicit 3.12
      # path install.ps1 provisions, ahead of the bare names, so a machine
      # carrying the aliases still resolves to a real interpreter.
      order="py|-3
${LOCALAPPDATA:-$HOME}/Programs/Python/Python312/python.exe|
python|
python3|"
      ;;
    *) order="python3|
python|
py|-3" ;;
  esac
  if pick_python; then
    py="$PICKED_PY"
    flag="$PICKED_FLAG"
    # Only an ABSOLUTE path is cacheable: reading the cache validates with
    # `[ -x ... ]`, which a bare name never satisfies, so caching one would
    # cost a re-probe every single call. The name-resolution fallback above is
    # rare enough to pay for itself each time.
    case "$py" in
      /*|[A-Za-z]:[\\/]*)
        ( umask 077; printf '%s\n%s\n%s\n' "$CACHE_FORMAT" "$py" "$flag" > "$cache" ) 2>/dev/null || true
        ;;
    esac
  fi
fi

# No Python at all: silence. The gate fails open by absence, exactly as its
# design rules say, and the builder sees nothing. The helpers Claude calls
# itself say so instead, so Claude never reports a decline as remembered when
# it was not.
if [ -z "$py" ]; then
  case "$sub" in
    memory|event|similar|logit) echo "Not run: no Python 3 found on this machine, so nothing was recorded or checked." ;;
    update) echo "Not run: no Python 3 found on this machine, so the toolkit was not updated. Suggest the builder tell the NSLS AI team." ;;
  esac
  exit 0
fi

# UTF-8 on every pipe. Claude Code speaks UTF-8 both ways, but Windows Python
# reads and writes a pipe in the ANSI code page (cp1252), and strictly. The
# session-start hook prints the guardrail policy, whose arrows cp1252 cannot
# encode, so on a PC the plugin copy died before the policy or the credit ping
# went out; the gate read the payload in cp1252 too. The settings.json shim
# always set this for its child; the plugin path never did.
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

if [ -n "$flag" ]; then
  "$py" "$flag" "$script" "$@"
else
  "$py" "$script" "$@"
fi
status=$?
[ "$status" = "2" ] && exit 2
exit 0
