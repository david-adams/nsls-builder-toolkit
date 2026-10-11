#!/bin/bash
# NSLS Builder Toolkit — one-command installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/thensls/nsls-builder-toolkit/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --test      # isolated "new user" test install
#
# What this does:
#   1. Installs the NSLS org skills (local plugin)
#   2. Installs superpowers + compound-engineering plugins (marketplace)
#   3. Tells you to run /nsls-setmeup to connect your tools

set -euo pipefail

# --- Config dir resolution -------------------------------------------------
# Claude Code respects CLAUDE_CONFIG_DIR everywhere; so does this installer, so
# everything lands in one place and stays consistent with how Claude Code is
# launched. `-t` / `--test` points that at a throwaway config dir
# ($HOME/.claude-kit-test by default, override with $CLAUDE_KIT_TEST_DIR) so you
# can install exactly like a brand-new user WITHOUT touching your real
# ~/.claude. To reset back to "new user", delete that dir. Launch a test
# install with:  CLAUDE_CONFIG_DIR="$HOME/.claude-kit-test" claude
TEST_MODE=0
for arg in "$@"; do
  case "$arg" in
    -t|--test) TEST_MODE=1 ;;
    *) ;;
  esac
done
if [ "$TEST_MODE" = "1" ] && [ -z "${CLAUDE_CONFIG_DIR:-}" ]; then
  export CLAUDE_CONFIG_DIR="${CLAUDE_KIT_TEST_DIR:-$HOME/.claude-kit-test}"
fi
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

PLUGIN_DIR="$CONFIG_DIR/local-plugins/nsls-builder-toolkit"
# Repo/branch are overridable for fork testing (e.g. install a feature branch
# from a fork before it merges to thensls/main). Defaults are production.
REPO_URL="${NSLS_TOOLKIT_REPO:-https://github.com/thensls/nsls-builder-toolkit.git}"
REPO_BRANCH="${NSLS_TOOLKIT_BRANCH:-main}"

echo ""
echo "=== NSLS Builder Toolkit ==="
if [ "$TEST_MODE" = "1" ]; then
  echo "  (TEST MODE — installing into $CONFIG_DIR; your real ~/.claude is untouched)"
fi
echo ""

# --- Prerequisites ---

if ! command -v git &>/dev/null; then
  echo "Error: git is not installed."
  echo "  macOS: Run 'xcode-select --install' first."
  exit 1
fi

if [ "$TEST_MODE" = "1" ]; then
  # Test config dir is fresh — create it so we install as a brand-new user.
  mkdir -p "$CONFIG_DIR"
elif [ ! -d "$CONFIG_DIR" ]; then
  echo "Error: Claude Code doesn't appear to be set up ($CONFIG_DIR not found)."
  echo "  Install Claude Code first, then re-run this script."
  exit 1
fi

# --- Step 1: Install the org toolkit ---

echo "Step 1: Installing org skills..."
mkdir -p "$CONFIG_DIR/local-plugins"

# A failed update must say so. With errors sent to /dev/null under set -e, a
# failed fetch used to end the installer with no message at all.
# A failed FETCH changes nothing on disk, so warn and carry on. A failed RESET
# may leave files half-updated, so that one stops rather than claim "unchanged".
# Only a real checkout counts as installed: an interrupted clone can leave a bare dir.
# -e, not -d: in a linked worktree .git is a file. (Not `git rev-parse`: that also
# says yes for an empty folder that merely sits inside some other repo.)
UPDATE_FAILED=0   # read by the closing banner, so it does not claim success
if [ -e "$PLUGIN_DIR/.git" ]; then
  echo "  Updating existing installation..."
  if ! UPD_ERR=$(git -C "$PLUGIN_DIR" fetch origin "$REPO_BRANCH" --quiet 2>&1); then
    echo "  WARNING: couldn't download the update, so your toolkit is UNCHANGED (still the version you had)."
    printf '%s\n' "$UPD_ERR" | tail -3 | sed 's/^/    /'
    # Name the likely cause: a test branch override beats "check your internet".
    if [ -n "${NSLS_TOOLKIT_BRANCH:-}" ]; then
      echo "  (You set NSLS_TOOLKIT_BRANCH=$NSLS_TOOLKIT_BRANCH: check that branch exists, then re-run this installer.)"
    else
      echo "  Check your internet connection, then re-run this installer."
    fi
    UPDATE_FAILED=1
  elif ! UPD_ERR=$(git -C "$PLUGIN_DIR" reset --hard "origin/$REPO_BRANCH" --quiet 2>&1); then
    echo "  ERROR: the update stopped partway, so the toolkit may be incomplete."
    printf '%s\n' "$UPD_ERR" | tail -3 | sed 's/^/    /'
    echo "  Re-run this installer to finish it."
    exit 1
  else
    echo "  Done."
  fi
else
  echo "  Cloning plugin..."
  if ! git clone --branch "$REPO_BRANCH" "$REPO_URL" "$PLUGIN_DIR" --quiet; then
    echo "  ERROR: couldn't download the toolkit. Check your internet connection, then re-run this installer."
    echo "  (If this keeps happening, delete the folder $PLUGIN_DIR and try again.)"
    exit 1
  fi
  echo "  Done."
fi
# The receipt: which code is actually installed.
echo "  Installed commit: $(git -C "$PLUGIN_DIR" log -1 --format='%h %s' 2>/dev/null || echo unknown)"

# --- Step 2: Enable the local plugin and register the auto-update hook in settings.json ---

SETTINGS="$CONFIG_DIR/settings.json"

# A fresh test config dir has no settings.json yet (a real user already has one
# from Claude Code). Seed an empty object so the merge below runs.
if [ "$TEST_MODE" = "1" ] && [ ! -f "$SETTINGS" ]; then echo '{}' > "$SETTINGS"; fi

# In test mode, seed a +test builder email so skill events land on a separate,
# obviously-test tracker row instead of polluting your real one (skill-event.sh
# would otherwise fall back to git config user.email). The +test address still
# reaches the proxy, so you can verify end-to-end delivery in Airtable.
if [ "$TEST_MODE" = "1" ]; then
  TEST_ENV_DIR="$CONFIG_DIR/local-plugins/nsls-personal-toolkit"
  if [ ! -f "$TEST_ENV_DIR/.env" ]; then
    mkdir -p "$TEST_ENV_DIR"
    REAL_EMAIL=$(git config user.email 2>/dev/null || echo "unknown@test.local")
    echo "BUILDER_EMAIL=${REAL_EMAIL%%@*}+test@${REAL_EMAIL#*@}" > "$TEST_ENV_DIR/.env"
    echo "  Test builder email: ${REAL_EMAIL%%@*}+test@${REAL_EMAIL#*@} (keeps test events off your real tracker row)"
  fi
fi

if [ -f "$SETTINGS" ]; then
  CONFIG_DIR="$CONFIG_DIR" python3 -c "
import json, os, sys
from pathlib import Path

CONFIG_DIR = os.environ['CONFIG_DIR']
SETTINGS_PATH = Path(CONFIG_DIR) / 'settings.json'
_SS = os.path.join(CONFIG_DIR, 'local-plugins/nsls-builder-toolkit/hooks/session-start.py')
HOOK_CMD = 'python3 -c \"exec(open(\'' + _SS + '\').read())\"'
HOOK_ENTRY = {
    'type': 'command',
    'command': HOOK_CMD,
    # Must exceed session-start.py's worst case: git pull (10s) + a replayed
    # ping (35s) + the live ping (35s). At 15s the hook was killed mid-delivery
    # on Railway cold starts (observed live 2026-07-03).
    'timeout': 90,
    'statusMessage': 'Syncing builder toolkit...'
}
MARKER = 'nsls-builder-toolkit/hooks/session-start.py'

# utf-8-sig tolerates a BOM: existing machines may have a BOM'd settings.json
# written by an older PowerShell installer, and plain utf-8 would choke on it.
with open(SETTINGS_PATH, encoding='utf-8-sig') as f: cfg = json.load(f)

# Hooks the toolkit plugin has already been seen running here. The migration
# retires those shims; re-adding them only undid it, and the next session start
# removed them again. plugin_beacon decides, as it does for the migration.
PROVEN = set()
try:
    os.environ['CLAUDE_CONFIG_DIR'] = CONFIG_DIR
    sys.path.insert(0, os.path.join(CONFIG_DIR, 'local-plugins/nsls-builder-toolkit/hooks'))
    import plugin_beacon
    PROVEN = set(plugin_beacon.proven())
except Exception:
    PROVEN = set()

# The toolkit is enabled by INSTALLING it: Step 3 runs the plugin install. This
# used to also write nsls-builder-toolkit@local into enabledPlugins, which never
# did anything -- there is no marketplace named local, so Claude Code cannot
# resolve the key -- but it looked like a live second installation, and that
# appearance cost two weeks of unguarded Macs: a test against it seemed to show
# that a plugin's bundled hooks do not load, which is why the guardrail gate was
# taken out of hooks.json. Machines that already carry the key have it removed
# by the migration, once the absence of that marketplace is confirmed.
#
# No backticks, no unescaped quotes, no bare dollar signs anywhere in this
# block, comments included: it is a Python program inside a shell double-quoted
# string, so the shell expands all three before Python ever sees them. An
# earlier draft of this very comment put a plugin-install command in
# backticks, and the shell ran it as a command substitution mid-install.

# Register auto-update hook (idempotent)
hooks = cfg.setdefault('hooks', {})
session_start = hooks.setdefault('SessionStart', [])

# Find or create the startup matcher entry
startup_entry = None
for entry in session_start:
    if entry.get('matcher', '').startswith('startup'):
        startup_entry = entry
        break
if startup_entry is None:
    startup_entry = {'matcher': 'startup', 'hooks': []}
    session_start.insert(0, startup_entry)

hook_list = startup_entry.setdefault('hooks', [])
existing = next((h for h in hook_list if MARKER in h.get('command', '')), None)
if 'session-start' in PROVEN:
    print('  Session hook: the toolkit plugin already runs it here, so no shim added')
elif existing is None:
    hook_list.insert(0, HOOK_ENTRY)
    print('  Registered session auto-update hook')
elif existing.get('timeout', 0) < HOOK_ENTRY['timeout']:
    # Repair installs registered when the budget was 15s — that killed the
    # hook mid-ping on cold starts.
    existing['timeout'] = HOOK_ENTRY['timeout']
    print('  Raised session hook timeout to', HOOK_ENTRY['timeout'])
else:
    print('  Auto-update hook already registered')

# Register the skill-event hook (PreToolUse:Skill) globally (idempotent).
# The plugin ships this in hooks/hooks.json, but a *locally enabled* plugin
# does not reliably load bundled hooks (especially on Claude Code desktop) —
# the same reason Step 3.5 has to register bundled MCP servers by hand. So we
# merge it into the global settings.json to make it the primary firing path.
# The server dedupes per builder/skill/day, so this is safe even on surfaces
# that also fire the plugin hook or a pointer file's inline bash.
SKILL_HOOK_CMD = 'bash ' + os.path.join(CONFIG_DIR, 'local-plugins/nsls-builder-toolkit/hooks/skill-event.sh')
SKILL_MARKER = 'nsls-builder-toolkit/hooks/skill-event.sh'
pre_tool_use = hooks.setdefault('PreToolUse', [])
skill_entry = None
for entry in pre_tool_use:
    if entry.get('matcher') == 'Skill':
        skill_entry = entry
        break
if skill_entry is None:
    skill_entry = {'matcher': 'Skill', 'hooks': []}
    pre_tool_use.append(skill_entry)
skill_hooks = skill_entry.setdefault('hooks', [])
# Friendly spinner text — without it, a new user only sees an opaque
# 'run bash skill-event.sh?' and it reads like a hidden script. This says
# plainly what it does: a one-line ping so their skill use is credited.
SKILL_STATUS = 'Logging skill use so you get NSLS credit (nothing else)…'
existing_skill = next((h for h in skill_hooks if SKILL_MARKER in h.get('command', '')), None)
if 'skill-event' in PROVEN:
    print('  Skill-event hook: the toolkit plugin already runs it here, so no shim added')
elif existing_skill is None:
    skill_hooks.append({'type': 'command', 'command': SKILL_HOOK_CMD,
                        'timeout': 5, 'statusMessage': SKILL_STATUS})
    print('  Registered skill-event hook (PreToolUse:Skill)')
else:
    # Backfill the friendly status message on installs that predate it.
    if existing_skill.get('statusMessage') != SKILL_STATUS:
        existing_skill['statusMessage'] = SKILL_STATUS
        print('  Updated skill-event hook status message')
    else:
        print('  Skill-event hook already registered')

# Register the guardrail gate (PreToolUse:Bash|Write|Edit) globally (idempotent).
# Same reasoning as the skill-event hook above: the plugin ships this in
# hooks/hooks.json, but a locally enabled plugin does not reliably load bundled
# hooks — which meant the four hard gates never ran for ANYONE. Proven
# 2026-09-01: a push from a personal repo full of NSLS fingerprints sailed
# through the live harness while the same payload piped into the script was
# denied, and the Events table held zero real guardrail_blocked rows ever.
# The gates were documentation. This registration is what makes them real.
# The quotes are escaped because this whole program is inside a shell
# double-quoted \`python3 -c \"...\"\`. Unescaped, they closed that string early:
# the shell then took the rest of the line as words to execute, and settings.json
# received the literal text \`python3  + os.path.join(CONFIG_DIR, local-plugins/...
# /guardrail-gate.py) + \` as the hook command. That is not a runnable command, so
# on every Mac the installer touched, the gate could not have fired even before
# the migration deleted it — and every matching tool call would have printed a
# hook-error notice. The session-start hook two blocks up escapes them correctly;
# this one did not. See test_installer_registration.py.
GATE_HOOK_CMD = 'python3 \"' + os.path.join(CONFIG_DIR, 'local-plugins/nsls-builder-toolkit/hooks/guardrail-gate.py') + '\"'
GATE_MARKER = 'nsls-builder-toolkit/hooks/guardrail-gate.py'
GATE_STATUS = 'Checking builder guardrails…'
gate_entry = None
for entry in pre_tool_use:
    if entry.get('matcher') == 'Bash|PowerShell|Write|Edit':
        gate_entry = entry
        break
if gate_entry is None:
    gate_entry = {'matcher': 'Bash|PowerShell|Write|Edit', 'hooks': []}
    pre_tool_use.append(gate_entry)
gate_hooks = gate_entry.setdefault('hooks', [])
if 'guardrail-gate' in PROVEN:
    print('  Guardrail gate: the toolkit plugin already runs it here, so no shim added')
elif next((h for h in gate_hooks if GATE_MARKER in h.get('command', '')), None) is None:
    gate_hooks.append({'type': 'command', 'command': GATE_HOOK_CMD,
                       'timeout': 10, 'statusMessage': GATE_STATUS})
    print('  Registered guardrail gate (PreToolUse:Bash|Write|Edit)')
else:
    print('  Guardrail gate already registered')

# Pre-authorize the hook command so a new builder is never confronted with a
# bare 'run this script?' prompt for our own tracking ping. The command is a
# fixed, known string (the hook we just registered), so an exact allow rule is
# safe and idempotent.
perms = cfg.setdefault('permissions', {})
allow = perms.setdefault('allow', [])
skill_allow_rule = 'Bash(' + SKILL_HOOK_CMD + ')'
if skill_allow_rule not in allow:
    allow.append(skill_allow_rule)
    print('  Allowlisted the skill-event hook (no more run-script prompt)')

with open(SETTINGS_PATH, 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=2)
" 2>/dev/null || echo "  Note: Could not update settings.json — add the hook manually"
else
  echo "  Note: No settings.json found — the plugin will be enabled on first use"
fi

# --- Step 2.5: Fire an install event to the Automation Tracker ---
#
# Records the install (and auto-registers brand-new builders — the server
# creates their row on first contact, so nobody is invisible until manually
# seeded). Email precedence matches the hooks: personal-toolkit .env →
# git config user.email → $USER@$HOSTNAME. Best-effort: a failed POST never
# blocks the install.

INSTALL_EMAIL=""
INSTALL_ENV_FILE="$CONFIG_DIR/local-plugins/nsls-personal-toolkit/.env"
if [ -f "$INSTALL_ENV_FILE" ]; then
  INSTALL_EMAIL=$(grep "^BUILDER_EMAIL=" "$INSTALL_ENV_FILE" | cut -d= -f2 | tr -d '"')
fi
[ -z "$INSTALL_EMAIL" ] && INSTALL_EMAIL=$(git config user.email 2>/dev/null || true)
[ -z "$INSTALL_EMAIL" ] && INSTALL_EMAIL="${USER:-unknown}@$(hostname -s 2>/dev/null || echo unknown)"

# Persist the EXACT provisional identity used for these early events so /nsls-setmeup
# Step 1.5 can reconcile them WITHOUT recomputing (parity with install.ps1).
# Written beside the toolkit, not in .env (which doesn't exist yet); gitignored.
# Idempotent — overwritten with the current value on every run.
printf '%s' "$INSTALL_EMAIL" > "$PLUGIN_DIR/.install-identity" 2>/dev/null || true

INSTALL_GH=$(gh api user --jq .login 2>/dev/null || true)

INSTALL_EMAIL_SAFE=$(printf '%s' "$INSTALL_EMAIL" | tr -d '"\\')
INSTALL_GH_SAFE=$(printf '%s' "$INSTALL_GH" | tr -d '"\\')
# --max-time must clear a Railway cold start (~35s, per session-start.py's own
# measurement); at 10s the very first install of the day — the one most worth
# recording — was silently dropped.
curl -s --max-time 40 -X POST \
  ${NSLS_TRACKER_URL:-https://web-production-6281e.up.railway.app}/install-event \
  -H 'Content-Type: application/json' \
  -d "{\"builder_email\":\"$INSTALL_EMAIL_SAFE\",\"github_username\":\"$INSTALL_GH_SAFE\",\"platform\":\"mac\",\"install_source\":\"cc-builder-kit\"}" \
  >/dev/null 2>&1 || true

# --- Step 3: Install marketplace plugins ---

echo ""
echo "Step 2: Installing recommended plugins..."

# Find the claude CLI — curl|bash may not inherit the full PATH
CLAUDE_BIN=""
for candidate in \
  "$(command -v claude 2>/dev/null)" \
  "$HOME/.local/bin/claude" \
  "$HOME/.claude/bin/claude" \
  "/usr/local/bin/claude" \
  "/opt/homebrew/bin/claude" \
  "$HOME/.npm-global/bin/claude" \
  "$HOME/.nvm/versions/node/*/bin/claude"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    CLAUDE_BIN="$candidate"
    break
  fi
done

# Retry with PATH refresh if not found (native installer may need a moment).
# The `|| true` is load-bearing: a bare VAR="$(failing command)" assignment
# carries the substitution's exit status, and under `set -e` that killed the
# whole install right here on every CLI-less Mac Desktop machine — before the
# desktop-app probe below ever ran (Mac round-2 finding C1).
if [ -z "$CLAUDE_BIN" ]; then
  eval "$(cat ~/.zshrc 2>/dev/null | grep -E 'export PATH|path=')" 2>/dev/null || true
  eval "$(cat ~/.bashrc 2>/dev/null | grep -E 'export PATH|path=')" 2>/dev/null || true
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi

# Desktop-app bundled CLI (no separate CLI install). macOS ships it under
# ~/Library/Application Support/Claude/claude-code[-vm]/<version>/claude.
# NOTE: on Apple Silicon that binary is a Linux build that runs inside the app's
# VM and is NOT executable from the host shell (verified: `exec format error`),
# so accept it ONLY if it actually runs. Otherwise fall through to the honest
# "no claude" path rather than selecting a binary that exec-fails on every call.
if [ -z "$CLAUDE_BIN" ]; then
  for base in \
    "$HOME/Library/Application Support/Claude/claude-code" \
    "$HOME/Library/Application Support/Claude/claude-code-vm"; do
    [ -d "$base" ] || continue
    # sort -V => highest version last (falls back gracefully if -V is unsupported).
    cand=$(ls -1d "$base"/*/claude 2>/dev/null | sort -V | tail -1) || true
    if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" --version >/dev/null 2>&1; then
      CLAUDE_BIN="$cand"
      break
    fi
  done
fi

# Set when a plugin the toolkit cannot work without fails to install, and
# reported again at the end. Nothing else reads it.
INSTALL_FAILED=""

install_plugin() {
  local name="$1"
  local install_cmd="$2"
  local marketplace_url="$3"
  local required="${4:-}"

  # A bare-name match is satisfied by a plugin of the same name from a
  # DIFFERENT marketplace, which would leave the machine with somebody else's
  # copy and none of this org's hooks or agents. When the caller names the
  # marketplace-qualified spec, confirm THAT key is the one registered; the
  # registry file is the only place the marketplace is recorded.
  if [ -n "$required" ]; then
    # For a plugin we cannot work without, ONLY the marketplace-qualified key
    # counts, and only from the registry, which is the single place the
    # marketplace is recorded. A bare-name match in `plugin list` is satisfied
    # by a same-named plugin from somebody else's marketplace, which leaves the
    # machine with their copy and none of this org's hooks or agents. No
    # fallback: if the registry cannot be read, the plugin is not proven
    # present, and installing again is harmless.
    if [ -r "${CONFIG_DIR:-}/plugins/installed_plugins.json" ] &&
       grep -q "\"$install_cmd\"" "${CONFIG_DIR:-}/plugins/installed_plugins.json"; then
      echo "  $name: already installed"
      return 0
    fi
  elif "$CLAUDE_BIN" plugin list 2>/dev/null | grep -q "$name"; then
    echo "  $name: already installed"
    return 0
  fi

  if [ -n "$marketplace_url" ]; then
    echo "  Adding $name marketplace..."
    "$CLAUDE_BIN" plugin marketplace add "$marketplace_url" 2>&1 | tail -1 || true
  fi
  echo "  Installing $name..."
  "$CLAUDE_BIN" plugin install "$install_cmd" 2>&1 | tail -1 || true

  # Verify instead of trusting. Piping to `tail -1` discards the install's exit
  # status, and `|| true` discarded what was left, so a marketplace that could
  # not be reached and an install that failed both printed the same line a
  # success prints. For the org toolkit that is the entire product silently
  # absent — no gates, no hooks, no agents — behind an installer that said it
  # worked. Ask the CLI what it actually has.
  if [ -n "$required" ]; then
    if [ -r "${CONFIG_DIR:-}/plugins/installed_plugins.json" ] &&
       grep -q "\"$install_cmd\"" "${CONFIG_DIR:-}/plugins/installed_plugins.json"; then
      return 0
    fi
  elif "$CLAUDE_BIN" plugin list 2>/dev/null | grep -q "$name"; then
    return 0
  fi

  if [ -n "$required" ]; then
    echo "  [!] $name did NOT install."
    echo "      The toolkit's hooks, guardrails and agents will be missing"
    echo "      until it does. Run this, then restart Claude Code:"
    echo "        claude plugin install $install_cmd"
    INSTALL_FAILED="${INSTALL_FAILED:+$INSTALL_FAILED }$name"
  else
    echo "  [warn] $name did not install; continuing without it."
  fi
  return 0
}

# Every renamed their marketplace from "every-marketplace" to
# "compound-engineering-plugin" (and bumped the plugin to 3.x). Builders who
# installed before the rename are pinned to the stale "every-marketplace"
# registration: install_plugin's name-only grep sees "compound-engineering"
# already in the list and never migrates them. This detects the old
# registration and clears it so the install below pulls the current plugin.
migrate_compound_marketplace() {
  "$CLAUDE_BIN" plugin marketplace list 2>/dev/null | grep -q "every-marketplace" || return 0
  echo "  Migrating compound-engineering off the renamed 'every-marketplace'..."

  # Disable across every scope first so nothing keeps the stale install pinned
  # (a shared project-scope enablement otherwise blocks uninstall).
  for scope in local project user; do
    "$CLAUDE_BIN" plugin disable compound-engineering@every-marketplace --scope "$scope" 2>/dev/null || true
  done

  # Uninstall the old plugin. Different CLI versions accept either the
  # marketplace-qualified spec or the bare manifest name, so try both — each is
  # idempotent and a no-op if the other already worked.
  "$CLAUDE_BIN" plugin uninstall compound-engineering@every-marketplace 2>/dev/null || true
  "$CLAUDE_BIN" plugin uninstall compound-engineering 2>/dev/null || true

  # Removing the marketplace is the reliable cleanup: it cascades and drops any
  # plugin still registered against it, so the migration completes even if the
  # uninstall calls above were no-ops.
  "$CLAUDE_BIN" plugin marketplace remove every-marketplace 2>/dev/null || true

  # Confirm the stale registration is actually gone before we reinstall.
  if "$CLAUDE_BIN" plugin marketplace list 2>/dev/null | grep -q "every-marketplace"; then
    echo "  Warning: could not fully remove 'every-marketplace'. Run manually:"
    echo "    claude plugin uninstall compound-engineering && claude plugin marketplace remove every-marketplace"
  fi
}

# --- Toolkit plugin pin (begin) ---
# The plugin must come from the same branch as the clone in Step 1. Without a
# pin, `marketplace add` takes the repo's default branch, so testing a branch
# ran the branch's hooks (the clone) with main's skills (the plugin). A local
# folder can't carry a pin: Claude Code reads it as it is on disk.
toolkit_market() {
  case "$REPO_URL" in
    http://*|https://*|ssh://*|git@*)
      if [ "$REPO_BRANCH" != "main" ]; then
        echo "$REPO_URL#$REPO_BRANCH"
        return 0
      fi ;;
  esac
  echo "$REPO_URL"
}

# install_plugin skips a plugin that is already installed, so on a machine that
# has the toolkit the pin above never took effect. Start the plugin over when
# it can't be what was asked for: on every run that names a branch or a repo
# (a branch moves without a version bump, which `plugin update` ignores), and
# on a plain run that finds a test branch or another repo still registered.
# The normal case, production and unpinned, is left alone.
repoint_toolkit_marketplace() {
  local registry="$CONFIG_DIR/plugins/known_marketplaces.json"
  [ -r "$registry" ] || return 0
  local verdict
  verdict=$(python3 - "$registry" "$REPO_URL" "${NSLS_TOOLKIT_BRANCH:-}${NSLS_TOOLKIT_REPO:-}" <<'PY' 2>/dev/null || true
import json, sys
registry, wanted, testing = sys.argv[1], sys.argv[2], sys.argv[3]
src = (json.load(open(registry, encoding="utf-8")).get("nsls-toolkit") or {}).get("source") or {}
if not src:
    print("absent")
    sys.exit()
url = src.get("url") or src.get("path") or ""
if not url and src.get("repo"):
    url = "https://github.com/" + src["repo"]
def norm(u):
    u = u.strip().rstrip("/").lower()
    return u[:-4] if u.endswith(".git") else u
print("repoint" if testing or src.get("ref") or norm(url) != norm(wanted) else "keep")
PY
)
  [ "$verdict" = "repoint" ] || return 0
  echo "  Pointing the toolkit plugin at $REPO_BRANCH..."
  "$CLAUDE_BIN" plugin marketplace remove nsls-toolkit >/dev/null 2>&1 || true
}

# The plugin's half of the receipt. On a test run, a plugin that isn't the
# commit the clone is on is the failure this block exists to prevent: say so.
toolkit_plugin_receipt() {
  local sha clone
  sha=$(python3 - "$CONFIG_DIR/plugins/installed_plugins.json" <<'PY' 2>/dev/null || true
import json, sys
for e in json.load(open(sys.argv[1], encoding="utf-8")).get("plugins", {}).get("nsls-builder-toolkit@nsls-toolkit", []):
    if e.get("gitCommitSha"):
        print(e["gitCommitSha"])
        break
PY
)
  [ -n "$sha" ] || return 0
  echo "  Plugin commit: ${sha:0:7}"
  clone=$(git -C "$PLUGIN_DIR" rev-parse HEAD 2>/dev/null || true)
  if [ -n "${NSLS_TOOLKIT_BRANCH:-}" ] && [ -n "$clone" ] && [ "$sha" != "$clone" ]; then
    echo "  [!] The plugin (${sha:0:7}) is not the commit you asked for (${clone:0:7})."
    echo "      Re-run this installer; if it happens again, the branch moved mid-install."
    INSTALL_FAILED="${INSTALL_FAILED:+$INSTALL_FAILED }nsls-builder-toolkit"
  fi
}
# --- Toolkit plugin pin (end) ---

if [ -n "$CLAUDE_BIN" ]; then
  # superpowers needs its marketplace registered first — a bare install spec
  # with no marketplace can never resolve on a fresh machine.
  # The org toolkit itself, as a real plugin. Until now neither installer did
  # this: on a Mac the self-migration installed it at the NEXT session start,
  # and on Windows nothing ever did — session-start.ps1 is the hook that fires
  # there and its only entry into Python runs the guardrails block, never
  # main(), so stage A was never called and no PC has ever had the plugin, its
  # three agents, or its bundled hooks. Installing it here makes both platforms
  # arrive in the same state on day one, and leaves the migration as the path
  # for machines installed before this change.
  # $REPO_URL, not the hard-coded production URL: NSLS_TOOLKIT_REPO exists so a
  # fork can be tested end to end, and cloning the fork while installing the
  # plugin from upstream tests two different revisions at once.
  repoint_toolkit_marketplace
  install_plugin "nsls-builder-toolkit" "nsls-builder-toolkit@nsls-toolkit" \
    "$(toolkit_market)" required
  toolkit_plugin_receipt

  install_plugin "superpowers" "superpowers@superpowers-marketplace" \
    "https://github.com/obra/superpowers-marketplace.git"
  migrate_compound_marketplace
  # Grep key is the bare plugin id (what `plugin list` prints); migrate_* above
  # has already cleared the stale every-marketplace install, so a bare-name
  # match here can only be the current compound-engineering-plugin one.
  install_plugin "compound-engineering" \
    "compound-engineering@compound-engineering-plugin" \
    "https://github.com/EveryInc/compound-engineering-plugin.git"
else
  echo ""
  echo "  Could not find the 'claude' CLI in PATH."
  echo "  After your next Claude Code session, run /nsls-setmeup — it will detect"
  echo "  missing plugins and give you the install commands."
  echo ""
  echo "  Or run these manually:"
  echo "    claude plugin install superpowers"
  echo "    claude plugin marketplace add https://github.com/EveryInc/compound-engineering-plugin.git"
  echo "    claude plugin install compound-engineering@compound-engineering-plugin"
fi

# --- Step 3.5: Register bundled MCP servers (signal, etc.) ---
#
# This installer also sets up the SHIM path: a clone plus hand-written
# settings.json entries, as the safety net for the window before the plugin's
# own hooks are proven on this machine. It used to additionally write
# `nsls-builder-toolkit@local` into enabledPlugins, which did nothing — there is
# no marketplace called `local`, so Claude Code cannot resolve the key. This
# comment used to say that key "loads skills/commands/hooks", and that sentence
# cost two weeks of unguarded Macs:
# an 2026-08-23 test against it appeared to prove that a plugin's bundled hooks
# do not load, which is why the guardrail gate was taken out of hooks.json on
# 2026-09-06. The marketplace-installed plugin loads its bundled hooks exactly
# as documented. What the inert key genuinely does not do is register bundled
# .mcp.json servers, so the signal_* tools never appear on this path.
#
# Fix: register each server from .mcp.json explicitly at user scope, pointing at
# the absolute install path. That path is the same local-plugins dir the
# auto-update hook git-pulls, so server updates still flow through on the next
# session. `claude mcp add` is idempotent — it no-ops if the server already
# exists, so re-running the installer is safe.

echo ""
echo "Step 3: Registering bundled MCP servers..."

# Once the toolkit plugin is installed it registers these servers itself; a
# user-scope copy beside it is the duplicate the migration removes. Step 2
# above installs the plugin, so this check comes after it.
if CLAUDE_CONFIG_DIR="$CONFIG_DIR" python3 "$PLUGIN_DIR/hooks/plugin_beacon.py" --installed 2>/dev/null | grep -qx 'installed'; then
  echo "  Skipped: the toolkit plugin registers its own MCP servers."
elif [ -n "$CLAUDE_BIN" ] && [ -f "$PLUGIN_DIR/.mcp.json" ]; then
  # Guard against set -e: a Python exception here (bad CLAUDE_BIN, malformed
  # .mcp.json) must not abort the installer and skip Steps 4-5, which don't
  # depend on MCP registration. Matches the || pattern used in Step 2.
  CLAUDE_BIN="$CLAUDE_BIN" PLUGIN_DIR="$PLUGIN_DIR" python3 - << 'PYEOF' || echo "  Note: MCP registration step failed — run /signal-setup later to register the server"
import json, os, re, subprocess, sys

claude = os.environ["CLAUDE_BIN"]
root = os.environ["PLUGIN_DIR"]

try:
    with open(os.path.join(root, ".mcp.json"), encoding="utf-8") as f:
        servers = json.load(f).get("mcpServers", {})
except Exception as e:
    print(f"  Could not read .mcp.json ({e}) — skipping MCP registration")
    sys.exit(0)

_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")

def sub(v):
    # The bundled config uses ${CLAUDE_PLUGIN_ROOT}; user-scope config doesn't
    # expand it, so substitute the real absolute path here. Also expand any
    # other ${ENV_VAR} that happens to be set in this shell (e.g. a pre-exported
    # studio token) — anything unset is left as a literal ${VAR} and caught by
    # unresolved() below.
    if not isinstance(v, str):
        return v
    v = v.replace("${CLAUDE_PLUGIN_ROOT}", root)
    return _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), v)

def unresolved(v):
    return isinstance(v, str) and bool(_VAR.search(v))

new = 0
skipped = []
needs_login = []
for name, cfg in servers.items():
    stype = cfg.get("type", "stdio")

    if stype == "http":
        # http servers take a URL, NOT a command. The old code built
        # `claude mcp add <name> --` with an empty command, which the CLI
        # rejects ("Command is required"). Correct form:
        # `claude mcp add --transport http <name> <url> [--header ...]`.
        url = sub(cfg.get("url", ""))
        headers = {k: sub(val) for k, val in cfg.get("headers", {}).items()}
        # A config MAY still pin a bearer token via ${SOME_TOKEN}, and those
        # don't exist on a fresh machine. Registering with an unexpanded token
        # yields a server that 401s silently — worse than not registering.
        if unresolved(url) or any(unresolved(h) for h in headers.values()):
            skipped.append(name)
            continue
        cmd = [claude, "mcp", "add", "--transport", "http", name, url,
               "--scope", "user"]
        for hk, hv in headers.items():
            cmd += ["--header", f"{hk}: {hv}"]
        # No auth header means the server authenticates by OAuth. Registration
        # succeeds either way, so nothing here fails — but until the person runs
        # `claude mcp login`, the server sits at "Needs authentication" and its
        # tools are simply absent. That reads as "the toolkit didn't install
        # anything", so say the next step out loud rather than leaving them to
        # notice a missing server.
        if not headers:
            needs_login.append(name)
    else:
        command = sub(cfg.get("command", ""))
        args = [sub(a) for a in cfg.get("args", [])]
        cmd = [claude, "mcp", "add", name, "--scope", "user",
               "--env", f"CLAUDE_PLUGIN_ROOT={root}"]
        for k, val in cfg.get("env", {}).items():
            cmd += ["--env", f"{k}={sub(val)}"]
        cmd += ["--", command] + args

    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    if "already exists" in out:
        print(f"  {name}: already registered")
    elif res.returncode == 0 and "Added" in out:
        print(f"  {name}: registered (user scope)")
        new += 1
    else:
        print(f"  {name}: registration failed — {out or 'unknown error'}")

print(f"  {new} MCP server(s) newly registered (restart Claude Code to load)")
if skipped:
    print(f"  Deferred (needs an access token): {', '.join(skipped)} — run /signal-setup to connect these.")
for name in needs_login:
    print(f"  {name}: sign in with  claude mcp login {name}  (opens your browser; no token to copy)")
PYEOF
else
  if [ -z "$CLAUDE_BIN" ]; then
    echo "  Skipped — 'claude' CLI not found in PATH. Run /signal-setup later to register."
  else
    echo "  No .mcp.json found — nothing to register"
  fi
fi

# --- Step 3.7: Install the gws CLI (Google Workspace) ---
#
# Two flagship skills — /gdoc-build and /gdoc-edit — shell out to `gws` and are
# dead on arrival without it. (Per-user OAUTH stays manual: `gws auth login`,
# run from those skills' setup.) Idempotent: skips if gws is already on PATH.
# Best-effort — a failed install must never abort the toolkit install.

echo ""
echo "Installing gws (Google Workspace CLI)..."
GWS_OK=0
if command -v gws &>/dev/null; then
  echo "  gws: already installed ($(gws --version 2>/dev/null | head -1))"
elif [ -x "$HOME/.local/bin/gws" ] && "$HOME/.local/bin/gws" --version >/dev/null 2>&1; then
  # A working gws already sits at the install target but ~/.local/bin isn't on
  # PATH, so `command -v` missed it. Do NOT re-download over it — the old code
  # overwrote this binary and then deleted it if the fresh download failed its
  # version check, destroying a working install. Just fix the PATH below.
  echo "  gws: already installed at ~/.local/bin (not on PATH — fixing that below)"
  GWS_OK=1
else
  # Upstream retired their installer script (the old
  # google-workspace-cli-installer.sh URL 404s — every fresh Mac install hit
  # it). Releases now ship per-arch tarballs with .sha256 sidecars: download
  # the right one, verify the checksum, install to ~/.local/bin (no sudo).
  GWS_OK=0
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)              GWS_TARGET="aarch64-apple-darwin" ;;
    Darwin-x86_64)             GWS_TARGET="x86_64-apple-darwin" ;;
    Linux-aarch64|Linux-arm64) GWS_TARGET="aarch64-unknown-linux-gnu" ;;
    Linux-x86_64)              GWS_TARGET="x86_64-unknown-linux-gnu" ;;
    *)                         GWS_TARGET="" ;;
  esac
  if [ -n "$GWS_TARGET" ]; then
    GWS_TMP=$(mktemp -d)
    GWS_TAR="google-workspace-cli-$GWS_TARGET.tar.gz"
    GWS_BASE="https://github.com/googleworkspace/cli/releases/latest/download"
    if curl --proto '=https' --tlsv1.2 -fsSL "$GWS_BASE/$GWS_TAR" -o "$GWS_TMP/$GWS_TAR" 2>/dev/null \
      && curl --proto '=https' --tlsv1.2 -fsSL "$GWS_BASE/$GWS_TAR.sha256" -o "$GWS_TMP/$GWS_TAR.sha256" 2>/dev/null; then
      # Tolerate both checksum-file formats ("<hex>" and "<hex>  <name>").
      GWS_EXPECTED=$(awk '{print $1}' "$GWS_TMP/$GWS_TAR.sha256" 2>/dev/null || true)
      if command -v shasum &>/dev/null; then
        GWS_ACTUAL=$(shasum -a 256 "$GWS_TMP/$GWS_TAR" | awk '{print $1}')
      else
        GWS_ACTUAL=$(sha256sum "$GWS_TMP/$GWS_TAR" | awk '{print $1}')
      fi
      if [ -n "$GWS_EXPECTED" ] && [ "$GWS_EXPECTED" = "$GWS_ACTUAL" ]; then
        if tar -xzf "$GWS_TMP/$GWS_TAR" -C "$GWS_TMP" 2>/dev/null; then
          GWS_BIN=$(find "$GWS_TMP" -type f -name gws 2>/dev/null | head -1)
          if [ -n "$GWS_BIN" ]; then
            mkdir -p "$HOME/.local/bin"
            mv "$GWS_BIN" "$HOME/.local/bin/gws" && chmod +x "$HOME/.local/bin/gws" && GWS_OK=1
            # Gate success on the binary actually running — a wrong-libc
            # artifact (e.g. the gnu build on musl/Alpine) exec-fails here,
            # and reporting it "installed" would be a lie.
            if [ "$GWS_OK" = "1" ] && ! "$HOME/.local/bin/gws" --version >/dev/null 2>&1; then
              GWS_OK=0
              echo "  Note: gws downloaded but won't run on this system (libc mismatch?) — removing it."
              rm -f "$HOME/.local/bin/gws"
            fi
          fi
        fi
      else
        echo "  Note: gws checksum verification failed — not installing this download."
      fi
    fi
    rm -rf "$GWS_TMP"
  fi
  if [ "$GWS_OK" = "1" ]; then
    echo "  gws installed to ~/.local/bin ($("$HOME/.local/bin/gws" --version 2>/dev/null | head -1)). Authenticate later with: gws auth login"
  else
    echo "  Note: gws install failed — /gdoc-build and /gdoc-edit will prompt you to install it."
  fi
fi

# Ensure ~/.local/bin is on PATH, idempotently. Shared by the gws step and the
# nsls-python launcher step below — both install there and both are invoked by
# bare name, so neither can depend on the other having done this.
# $1 = what needs it, for the message ("gws", "the document skills").
ensure_local_bin_on_path() {
  local what="${1:-the toolkit}"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) return 0 ;;
  esac
  if [ "$TEST_MODE" = "1" ]; then
    echo "  Note: ~/.local/bin isn't on your PATH — add it to use $what (test mode won't touch your shell profile)."
    return 0
  fi
  # Pick the rc file the user's LOGIN SHELL actually reads. Selecting by
  # file-existence order (zshrc, then bashrc, then bash_profile) wrote the
  # export into ~/.bashrc for any zsh user who merely happened to have
  # one — and zsh never sources it, so gws stayed missing while the
  # installer reported PATH configured.
  local sh_name rc
  sh_name=$(basename "${SHELL:-}" 2>/dev/null || true)
  if [ -z "$sh_name" ] || [ "$sh_name" = "sh" ]; then
    case "$(uname -s)" in
      Darwin) sh_name="zsh" ;;
      *)      sh_name="bash" ;;
    esac
  fi
  rc=""
  case "$sh_name" in
    zsh) rc="${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash)
      # bash reads .bashrc for interactive non-login shells, but macOS
      # Terminal starts login shells, which read .bash_profile instead.
      # So on macOS always use .bash_profile, even when a .bashrc exists.
      case "$(uname -s)" in
        Darwin) rc="$HOME/.bash_profile" ;;
        *)
          if [ -f "$HOME/.bashrc" ]; then rc="$HOME/.bashrc"
          elif [ -f "$HOME/.bash_profile" ]; then rc="$HOME/.bash_profile"
          else rc="$HOME/.bashrc"
          fi
          ;;
      esac
      ;;
    *) rc="" ;;  # fish/nu/etc: different syntax — instruct, don't corrupt
  esac
  if [ -z "$rc" ]; then
    echo "  Note: ~/.local/bin isn't on your PATH, and $sh_name needs a syntax this installer doesn't write."
    echo "        Add ~/.local/bin to your PATH manually to use $what."
    return 0
  fi
  [ -f "$rc" ] || touch "$rc" 2>/dev/null || {
    echo "  Note: couldn't write $(basename "$rc") — add ~/.local/bin to your PATH manually to use $what."
    return 0
  }
  # Match the exact export line, not the bare substring: a commented-out
  # export, or an unrelated path that merely contains ".local/bin"
  # (/opt/app/.local/bin), would satisfy a substring grep and we'd skip
  # appending — leaving it off PATH while reporting it configured.
  if ! grep -qE '^[[:space:]]*export PATH="\$HOME/\.local/bin' "$rc" 2>/dev/null; then
    if { echo ""; echo "# NSLS Builder Toolkit"; echo 'export PATH="$HOME/.local/bin:$PATH"'; } >> "$rc" 2>/dev/null; then
      echo "  Added ~/.local/bin to PATH in $(basename "$rc") (takes effect in new terminals)."
    else
      echo "  Note: couldn't update $(basename "$rc") — add ~/.local/bin to your PATH manually to use $what."
    fi
  fi
  return 0
}

# PATH fix-up, outside the install branch so it also runs for a gws we FOUND at
# ~/.local/bin rather than downloaded (that's the whole point of the elif above).
if [ "$GWS_OK" = "1" ]; then
  ensure_local_bin_on_path "gws"
fi

# --- Step 3.75: Python libraries for the document skills ---
#
# /gdoc-build (python-docx) and /nsls-slides (python-pptx) are dead on arrival
# without these, and until now install.sh provisioned neither — so a builder's
# first /gdoc-build ended in "ModuleNotFoundError: No module named 'docx'" or
# "python3.12: command not found", and Claude told them to go install Python.
# install.ps1 has always done this on Windows (Step 0); this is the Mac/Linux
# half of that parity.
#
# Two things make it silent:
#   1. Libraries land in the DURABLE ~/.local/lib/nsls-pydeps, not /tmp (macOS
#      /tmp cleanup gutted the old /tmp/pptx_deps between sessions).
#   2. A tiny `nsls-python` launcher pins the interpreter choice ONCE, here,
#      instead of every skill hardcoding python3.12 and breaking on the many
#      Macs that only ship a newer 3.x. python-docx/pptx are pure-Python and
#      run fine on 3.10-3.14.
# Best-effort throughout: a failure here must never abort the toolkit install.

echo ""
echo "Installing Python libraries for the document skills..."

PYDEPS_DIR="$HOME/.local/lib/nsls-pydeps"
NSLS_PY=""

# Prefer 3.12 (the most-tested version across the toolkit's skills), then walk
# newer-to-older. Plain `python3` is the last resort and only if it's >= 3.10.
for candidate in python3.12 python3.13 python3.11 python3.14 python3.10 python3; do
  command -v "$candidate" &>/dev/null || continue
  # Stock-macOS /usr/bin/python3 without Command Line Tools is a stub that
  # prompts a GUI install popup, so gate on actually running.
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' &>/dev/null; then
    NSLS_PY="$candidate"
    break
  fi
done

if [ -z "$NSLS_PY" ]; then
  # No usable interpreter. Say what breaks, don't auto-install one: brew/pkg
  # installs are slow and can raise OS permission prompts mid-install.
  echo "  Note: no Python 3.10+ found — /gdoc-build and /nsls-slides will be unavailable."
  if command -v brew &>/dev/null; then
    echo "        Install it:  brew install python@3.12   (then re-run this installer)"
  else
    echo "        Install Python 3.12 from https://www.python.org/downloads/, then re-run this installer."
  fi
else
  # EVERY fallible operation below is guarded. This step is best-effort by
  # design, and `set -euo pipefail` would otherwise turn an unwritable
  # ~/.local/lib (or a full /tmp) into an aborted toolkit install with a raw
  # "mkdir: Permission denied" as the builder's last word — verified: it did.
  PY_VER=$("$NSLS_PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo "unknown")
  # Guard on a real import, never on a directory: /tmp cleanup (and a partial
  # pip run) leaves the dirs behind, so a -d check passes on a gutted install.
  if PYTHONPATH="$PYDEPS_DIR" "$NSLS_PY" -c 'import docx, pptx' &>/dev/null; then
    echo "  Python libraries: already installed (python $PY_VER)"
  elif ! mkdir -p "$PYDEPS_DIR" 2>/dev/null; then
    echo "  Note: can't create ~/.local/lib/nsls-pydeps (permissions?) — /gdoc-build"
    echo "        and /nsls-slides will be unavailable. Everything else installs fine."
  else
    # --upgrade is load-bearing: without it pip sees a damaged package already
    # in the target, exits 0 without writing, and the next build raises the
    # same ImportError. A PEP 668 "externally-managed-environment" python
    # (Homebrew, Debian) refuses even a --target install, so retry with
    # --break-system-packages — which touches nothing system-wide here,
    # because --target keeps every file inside our own directory.
    PIP_LOG=$(mktemp 2>/dev/null || echo "")
    if ! "$NSLS_PY" -m pip install --upgrade python-docx python-pptx \
          --target "$PYDEPS_DIR" -q >"${PIP_LOG:-/dev/null}" 2>&1; then
      "$NSLS_PY" -m pip install --upgrade python-docx python-pptx \
          --target "$PYDEPS_DIR" --break-system-packages -q >"${PIP_LOG:-/dev/null}" 2>&1 || true
    fi
    if PYTHONPATH="$PYDEPS_DIR" "$NSLS_PY" -c 'import docx, pptx' &>/dev/null; then
      echo "  Python libraries installed to ~/.local/lib/nsls-pydeps (python $PY_VER)"
    else
      echo "  Note: python-docx/python-pptx install failed — /gdoc-build and /nsls-slides"
      echo "        will be unavailable. Everything else installs fine."
      if [ -n "$PIP_LOG" ]; then
        echo "        Last pip output:"
        { tail -3 "$PIP_LOG" 2>/dev/null || true; } | sed 's/^/          /' || true
      fi
    fi
    [ -n "$PIP_LOG" ] && rm -f "$PIP_LOG"
  fi

  # The launcher every document skill calls: the right interpreter with the
  # toolkit's libraries already importable. One place to fix, not one per skill.
  # Rewritten every run so a Python upgrade (3.12 -> 3.13) self-heals silently
  # on the next install/auto-update instead of stranding the skills.
  NSLS_PY_ABS=$(command -v "$NSLS_PY" 2>/dev/null || echo "")
  LAUNCHER="$HOME/.local/bin/nsls-python"
  # Write to a temp file and rename over the launcher only once it's complete,
  # so an interrupted write can never leave a truncated nsls-python behind.
  LAUNCHER_TMP="$LAUNCHER.tmp.$$"
  if [ -z "$NSLS_PY_ABS" ] || ! mkdir -p "$HOME/.local/bin" 2>/dev/null; then
    echo "  Note: couldn't create ~/.local/bin — the document skills will fall back"
    echo "        to a plain python3. Re-run this installer to fix it."
  # The braces matter: `cat ... 2>/dev/null` silences cat, but a FAILED
  # REDIRECTION is reported by the shell itself, so an unwritable ~/.local/bin
  # leaked a raw "Permission denied" line. Grouping puts the redirect inside the
  # suppressed scope.
  elif ! { cat > "$LAUNCHER_TMP" << NSLSPYEOF
#!/bin/sh
# nsls-python — the Python the NSLS toolkit's document skills run on.
# Generated by install.sh; regenerated on every install/update. Don't edit.
# Interpreter resolved at install time: $NSLS_PY_ABS (python $PY_VER)
PYDEPS="\$HOME/.local/lib/nsls-pydeps"
# Keep any caller-supplied PYTHONPATH, ours first. Never /tmp: anyone on the
# machine can write there, so loading libraries from it would run their code.
if [ -n "\${PYTHONPATH:-}" ]; then
  PYTHONPATH="\$PYDEPS:\$PYTHONPATH"
else
  PYTHONPATH="\$PYDEPS"
fi
export PYTHONPATH
if [ -x "$NSLS_PY_ABS" ]; then
  exec "$NSLS_PY_ABS" "\$@"
fi
# The recorded interpreter is gone (uninstalled, or a brew upgrade moved it).
# Re-apply the SAME gate the installer used rather than exec'ing blind: a stock
# macOS stub or an ancient 3.x would otherwise produce a baffling error.
# A replacement only counts if it can actually import the libraries: they were
# built for the old Python, and compiled parts (lxml) won't load in a new one.
for _c in python3.12 python3.13 python3.11 python3.14 python3.10 python3; do
  command -v "\$_c" >/dev/null 2>&1 || continue
  if "\$_c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 \\
     && "\$_c" -c 'import docx, pptx' >/dev/null 2>&1; then
    exec "\$_c" "\$@"
  fi
done
echo "nsls-python: the Python this was installed with is gone, and no other Python" >&2
echo "can load its libraries. Re-run the NSLS toolkit installer to repair it." >&2
exit 127
NSLSPYEOF
  } 2>/dev/null
  then
    rm -f "$LAUNCHER_TMP" 2>/dev/null
    echo "  Note: couldn't write the nsls-python launcher — re-run this installer to fix it."
  elif ! chmod +x "$LAUNCHER_TMP" 2>/dev/null; then
    rm -f "$LAUNCHER_TMP" 2>/dev/null
    echo "  Note: couldn't install the nsls-python launcher — re-run this installer."
  # Test the NEW launcher before it replaces anything: a failed provisioning run
  # must never swap out a launcher that was working. And claim "ready" only off
  # a real import, never off a successful file-write.
  elif ! "$LAUNCHER_TMP" -c 'import docx, pptx' &>/dev/null; then
    rm -f "$LAUNCHER_TMP" 2>/dev/null
    if [ -x "$LAUNCHER" ] && "$LAUNCHER" -c 'import docx, pptx' &>/dev/null; then
      echo "  Note: couldn't refresh the nsls-python launcher, so your existing one (which works) was kept."
    else
      echo "  Note: the document skills' Python libraries aren't importable yet."
      echo "        /gdoc-build and /nsls-slides will be unavailable until this installer is re-run; everything else is fine."
    fi
  elif ! mv -f "$LAUNCHER_TMP" "$LAUNCHER" 2>/dev/null; then
    rm -f "$LAUNCHER_TMP" 2>/dev/null
    echo "  Note: couldn't install the nsls-python launcher — re-run this installer."
  else
    echo "  nsls-python launcher ready (the document skills call this)."
  fi

  # Put ~/.local/bin on PATH for OUR launcher, independent of whether the gws
  # step above happened to do it — the skills invoke `nsls-python` by bare name,
  # so a machine that already had gws elsewhere would otherwise get a launcher
  # nothing can reach.
  ensure_local_bin_on_path "the document skills"
fi

# --- Step 3.8: Node.js check (the signal MCP server needs it) ---
# Instruct + degrade (matches this script's style); don't auto-install Node.
echo ""
if command -v node &>/dev/null; then
  echo "Node.js: $(node --version 2>/dev/null) — the signal MCP server can run."
else
  echo "Node.js not found — the 'signal' MCP server needs it to connect."
  if command -v brew &>/dev/null; then
    echo "  Install it:  brew install node   (then restart Claude Code and run /signal-setup)"
  else
    echo "  Install Node LTS from https://nodejs.org, then restart Claude Code and run /signal-setup."
  fi
fi

# --- Step 4: Create slash-command pointer skills ---

echo ""
echo "Step 4: Creating slash-command pointers..."
SKILLS_DIR="$CONFIG_DIR/skills"
mkdir -p "$SKILLS_DIR"
# Once the plugin's session start is proven here, the plugin delivers these
# skills and the migration retires the pointers. Writing them again only put
# every skill in the list twice until the next session start.
PLUGIN_SERVES_SKILLS=""
if CLAUDE_CONFIG_DIR="$CONFIG_DIR" python3 "$PLUGIN_DIR/hooks/plugin_beacon.py" --proven 2>/dev/null | grep -qx 'session-start'; then
  PLUGIN_SERVES_SKILLS=1
  echo "  Skipped: the toolkit plugin provides these skills on this machine."
fi

# True only if file $1 is exactly the toolkit's pointer to skill $2. Anything
# else may be a skill the builder wrote, even one that mentions a toolkit path.
# The check is is_own_pointer() in hooks/session-start.py, the one definition
# all three pointer writers share; if it cannot run, the file is left alone.
is_own_pointer() {
  python3 - "$1" "$2" "$PLUGIN_DIR/hooks/session-start.py" << 'PYEOF' 2>/dev/null
import importlib.util, sys
path, skill, hook = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("nsls_session_start", hook)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with open(path, encoding="utf-8") as f:
    text = f.read()
sys.exit(0 if mod.is_own_pointer(text, skill, ("nsls-builder-toolkit",)) else 1)
PYEOF
}

count=0
for skill_dir in "$PLUGIN_DIR/skills"/*/; do
  [ -n "$PLUGIN_SERVES_SKILLS" ] && break
  skill=$(basename "$skill_dir")
  dest="$SKILLS_DIR/$skill"
  src="$skill_dir/SKILL.md"
  [ -f "$src" ] || continue

  # Skip if the builder already has their own skill with this name
  if [ -d "$dest" ] && [ -f "$dest/SKILL.md" ]; then
    is_own_pointer "$dest/SKILL.md" "$skill" || continue
  fi

  # Extract name from frontmatter
  name=$(grep "^name:" "$src" | head -1 | sed 's/name: *//')
  [ -z "$name" ] && continue

  # Extract description (handles >- multiline format)
  desc=$(python3 -c "
import re, sys
with open('$src', encoding='utf-8') as f: content = f.read()
fm = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
if not fm: sys.exit(0)
m = re.search(r'description:\s*>-?\s*\n((?:[ \t]+.+\n?)*)', fm.group(1))
if m: d = ' '.join(m.group(1).split())
else:
    m = re.search(r'description:[ \t]*(.+)', fm.group(1), re.MULTILINE)
    d = m.group(1).strip() if m else ''
    # A bare block indicator is not a description; and strip YAML quotes off a
    # single-line scalar, decoding double-quoted escapes in ONE left-to-right
    # pass (chained replaces would re-decode their own output). chr(34)/chr(39)
    # /chr(92) are a double-quote, single-quote and backslash: this Python is
    # embedded in a double-quoted shell string, where a literal double-quote
    # would end it early.
    if d in ('>', '>-', '>+', chr(124), chr(124) + '-', chr(124) + '+'):
        d = ''
    else:
        q = d[:1]
        if len(d) > 1 and d[-1:] == q and q in (chr(34), chr(39)):
            inner = d[1:-1]
            if q == chr(34):
                # Includes YAML's four named Unicode escapes (N _ L P);
                # the whitespace collapse folds all four to spaces.
                simple = {'0': chr(0), 'a': chr(7), 'b': chr(8), 't': chr(9),
                          'n': chr(10), 'v': chr(11), 'f': chr(12), 'r': chr(13),
                          'e': chr(27), 'N': chr(133), '_': chr(160),
                          'L': chr(8232), 'P': chr(8233)}
                esc = re.compile(chr(92) * 2 + 'x([0-9a-fA-F]{2})|' + chr(92) * 2
                                 + 'u([0-9a-fA-F]{4})|' + chr(92) * 2
                                 + 'U([0-9a-fA-F]{8})|' + chr(92) * 2 + '(.)')
                def rep(mm):
                    for g in (1, 2, 3):
                        if mm.group(g): return chr(int(mm.group(g), 16))
                    return simple.get(mm.group(4), mm.group(4))
                inner = esc.sub(rep, inner)
            else:
                inner = inner.replace(chr(39) * 2, chr(39))
            d = inner
    # Map decoded control chars (NUL, BEL, ESC) to spaces -- they would make the
    # generated pointer unparseable -- then collapse whitespace, because the
    # caller embeds this as one indented line under description: >-.
    d = re.sub('[' + chr(92) + 'x00-' + chr(92) + 'x08' + chr(92) + 'x0b' + chr(92) + 'x0c' + chr(92) + 'x0e-' + chr(92) + 'x1f' + chr(92) + 'x7f-' + chr(92) + 'x9f]', ' ', d)
    d = ' '.join(d.split())
if d: print(d)
" 2>/dev/null)
  [ -z "$desc" ] && desc="NSLS Builder Toolkit skill: $skill"

  mkdir -p "$dest"
  cat > "$dest/SKILL.md" << POINTER
---
name: $name
description: >-
  $desc
---

Read and follow the full skill at \`$PLUGIN_DIR/skills/$skill/SKILL.md\`.
POINTER
  count=$((count + 1))
done

echo "  $count skill pointers synced"

# --- Step 5: Add 'cc' shortcut ---

echo ""
echo "Step 5: Adding 'cc' shortcut..."

if [ "$TEST_MODE" = "1" ]; then
  # Never touch the real shell profile in test mode — the test install must be
  # fully contained in $CONFIG_DIR and leave no trace outside it.
  echo "  Skipped in test mode (won't modify your shell profile)."
else
  # Detect shell config file
  SHELL_RC=""
  if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
  elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
  elif [ -f "$HOME/.bash_profile" ]; then
    SHELL_RC="$HOME/.bash_profile"
  fi

  if [ -n "$SHELL_RC" ]; then
    if grep -q "alias cc=" "$SHELL_RC" 2>/dev/null; then
      echo "  cc shortcut: already configured"
    else
      echo "" >> "$SHELL_RC"
      echo "# Claude Code shortcut" >> "$SHELL_RC"
      echo "alias cc='claude'" >> "$SHELL_RC"
      echo "  Added 'cc' shortcut to $(basename "$SHELL_RC") — type cc to launch Claude Code"
      echo "  (takes effect in new terminal windows, or run: source $SHELL_RC)"
    fi
  else
    echo "  Could not find shell config (.zshrc, .bashrc, .bash_profile)"
    echo "  Add this manually: alias cc='claude'"
  fi
fi

# --- Done ---

echo ""
echo "==============================="
if [ -n "$INSTALL_FAILED" ]; then
  echo "  NSLS Builder Toolkit set up, but this did NOT install: $INSTALL_FAILED (see [!] above)."
elif [ "$UPDATE_FAILED" = 1 ]; then
  echo "  NSLS Builder Toolkit set up, but NOT updated (see the WARNING above)."
else
  echo "  NSLS Builder Toolkit installed!"
fi
echo "==============================="
echo ""
echo "What you got:"
echo ""
SKILL_COUNT=$(ls "$PLUGIN_DIR/skills/" 2>/dev/null | wc -l | tr -d ' ')
echo "  ORG SKILLS ($SKILL_COUNT skills for building, tracking, deploying):"
ls "$PLUGIN_DIR/skills/" | sed 's/^/    \//'
echo ""
if [ -n "$CLAUDE_BIN" ]; then
  echo "  PLUGINS:"
  echo "    superpowers              — planning, debugging, verification workflows"
  echo "    compound-engineering     — brainstorm, plan, build, review pipeline"
  echo ""
else
  # Honest banner: without the CLI, plugins + MCP registration were skipped —
  # don't imply they landed. Name what's missing and how to finish.
  echo "  NOTE: the 'claude' CLI wasn't found, so these were SKIPPED:"
  echo "    - plugins (superpowers, compound-engineering) — NOT installed"
  echo "    - bundled MCP servers (e.g. signal) — NOT registered"
  echo "  Finish them after your first Claude Code session by running:  /nsls-setmeup"
  echo ""
fi
if [ "$TEST_MODE" != "1" ]; then
  echo "  SHORTCUT:"
  echo "    cc                       — type 'cc' in any terminal to launch Claude Code"
  echo ""
fi

if [ "$TEST_MODE" = "1" ]; then
  echo "=== TEST INSTALL — how to run and reset ==="
  echo ""
  echo "  Everything went into:  $CONFIG_DIR"
  echo "  Your real ~/.claude was NOT touched."
  echo ""
  echo "  NOTE: --test is terminal-only. The desktop app always launches"
  echo "  against your real ~/.claude and cannot open this isolated config,"
  echo "  so a test install can only be exercised from the terminal as below."
  echo ""
  echo "  1. Launch Claude Code against this test install:"
  echo ""
  echo "       CLAUDE_CONFIG_DIR=\"$CONFIG_DIR\" claude"
  echo ""
  echo "  2. Try it: say  /nsls-setmeup  (or  open day )  as a first-time user would."
  echo ""
  echo "  3. Reset back to a brand-new user (wipes the test install only):"
  echo ""
  echo "       rm -rf \"$CONFIG_DIR\""
  echo ""
  echo "     Then re-run this installer with --test to start clean again."
  echo ""
else
  if [ -n "$INSTALL_FAILED" ]; then
    echo "=== SOMETHING DID NOT INSTALL ==="
    echo ""
    echo "  Missing: $INSTALL_FAILED"
    echo "  Fix that first — the steps below assume the toolkit is present."
    echo ""
  fi
  echo "=== NEXT STEP ==="
  echo ""
  echo "  1. Restart Claude Code"
  echo "       Desktop app: quit and reopen it, then click Code (top left)."
  echo "       Terminal:    open a new window and type  cc"
  echo "     (A restart is required to load the MCP servers and hooks.)"
  echo ""
  echo "  2. Say:  /nsls-setmeup"
  echo "     This connects your tools (Slack, Google Drive, Calendar, Gmail,"
  echo "     Fathom — one at a time, with you) and optionally installs personal"
  echo "     productivity skills (daily planning, weekly reviews, project logging)."
  echo ""
fi

# A known failure exits 1, so a wrapper or CI can tell (the banner above already
# says what is missing).
[ -z "$INSTALL_FAILED" ] || exit 1
