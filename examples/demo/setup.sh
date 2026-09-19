#!/usr/bin/env bash
# Build a throwaway demo repository for recording the ai-push-hooks demo.
#
#   ./setup.sh [target-dir]      default: ~/aph-demo
#   ./setup.sh --force [dir]     rebuild a dir a previous run created
#
# Creates <target>/work (the demo repo), <target>/origin.git (a bare remote so
# `git push` really fires the pre-push hook), and <target>/bin/{use,reset,card}.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER=".ai-push-hooks-demo"
FORCE=0

if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
  shift
fi
TARGET="${1:-$HOME/aph-demo}"

# --- resolve the ai-push-hooks executable the installed hook will call --------
if [[ -n "${AI_PUSH_HOOKS_BIN:-}" ]]; then
  APH_BIN="$AI_PUSH_HOOKS_BIN"
elif [[ -x "$SOURCE_DIR/../../.venv/bin/ai-push-hooks" ]]; then
  APH_BIN="$(cd "$SOURCE_DIR/../../.venv/bin" && pwd)/ai-push-hooks"
elif command -v ai-push-hooks >/dev/null 2>&1; then
  APH_BIN="$(command -v ai-push-hooks)"
else
  echo "setup: no ai-push-hooks executable found." >&2
  echo "setup: set AI_PUSH_HOOKS_BIN=/path/to/ai-push-hooks and retry." >&2
  exit 1
fi
echo "setup: using runner binary $APH_BIN"

# --- refuse to clobber anything this script did not create -------------------
if [[ -e "$TARGET" ]]; then
  if [[ $FORCE -eq 1 && -f "$TARGET/$MARKER" ]]; then
    echo "setup: removing previous demo at $TARGET"
    rm -rf "$TARGET"
  elif [[ -f "$TARGET/$MARKER" ]]; then
    echo "setup: $TARGET already exists. Re-run with --force to rebuild it." >&2
    exit 1
  else
    echo "setup: $TARGET exists and was not created by this script. Refusing." >&2
    exit 1
  fi
fi

mkdir -p "$TARGET"
touch "$TARGET/$MARKER"
WORK="$TARGET/work"
ORIGIN="$TARGET/origin.git"

git init -q --bare "$ORIGIN"
mkdir -p "$WORK"
cp -R "$SOURCE_DIR/fixture/." "$WORK/"
cp "$SOURCE_DIR/configs/act1-detect.toml" "$WORK/ai-push-hooks.toml"

cd "$WORK"
git init -q -b main
git config user.name "Demo Dev"
git config user.email "demo@example.com"
git config commit.gpgsign false
git add -A
git commit -qm "orders-service: initial commit"
git remote add origin "$ORIGIN"
git push -q -u origin main

# --- the compliant branch ----------------------------------------------------
git checkout -q -b feature/clean
cp -R "$SOURCE_DIR/branches/clean/." "$WORK/"
git add -A
git commit -qm "invoices: add invoice card and currency column"

# --- the branch an agent wrote carelessly ------------------------------------
git checkout -q main
git checkout -q -b feature/violations
cp -R "$SOURCE_DIR/branches/violations/." "$WORK/"
git add -A
git commit -qm "refunds: add refund panel, drop legacy total, document refunds"

"$APH_BIN" install >/dev/null
echo "setup: installed pre-push hook"

# --- short commands for on-camera use ----------------------------------------
mkdir -p "$TARGET/bin"

cat >"$TARGET/bin/use" <<EOF
#!/usr/bin/env bash
# use act1|act2|act3|act4|extra -- swap in an act's workflow, commit the swap.
set -euo pipefail
case "\${1:-}" in
  act1) file=act1-detect.toml;     note="review the diff against AGENTS.md, block on findings" ;;
  act2) file=act2-fix.toml;        note="add a scoped apply step" ;;
  act3) file=act3-verify.toml;     note="verify the fix with the project's own checks" ;;
  act4) file=act4-two-models.toml; note="review with opencode, fix with claude" ;;
  extra) file=extra-autocommit.toml; note="commit and push the fix automatically" ;;
  *) echo "usage: use act1|act2|act3|act4|extra" >&2; exit 1 ;;
esac
cd "$WORK"
cp "$SOURCE_DIR/configs/\$file" ai-push-hooks.toml
if ! git diff --quiet -- ai-push-hooks.toml; then
  git add ai-push-hooks.toml
  git commit -qm "workflow: \$note"
fi
echo "workflow: \$file"
EOF

cat >"$TARGET/bin/reset" <<EOF
#!/usr/bin/env bash
# reset -- rebuild feature/violations from main so an act can be re-recorded.
set -euo pipefail
cd "$WORK"
git checkout -q main
git branch -q -D feature/violations 2>/dev/null || true
git push -q origin --delete feature/violations 2>/dev/null || true
git checkout -q -b feature/violations
cp -R "$SOURCE_DIR/branches/violations/." "$WORK/"
cp "$SOURCE_DIR/configs/act1-detect.toml" ai-push-hooks.toml
git add -A
git commit -qm "refunds: add refund panel, drop legacy total, document refunds"
rm -rf .git/ai-push-hooks
echo "reset: feature/violations rebuilt, artifacts cleared"
EOF

cat >"$TARGET/bin/card" <<'EOF'
#!/usr/bin/env bash
# card "text" -- a full-screen title card, since the recording has no audio.
set -euo pipefail
clear
printf '\n\n'
printf '  \033[1;36m%s\033[0m\n' "$1"
[[ -n "${2:-}" ]] && printf '  \033[2m%s\033[0m\n' "$2"
printf '\n'
sleep "${CARD_SECONDS:-2.5}"
EOF

chmod +x "$TARGET/bin/use" "$TARGET/bin/reset" "$TARGET/bin/card"

cat <<EOF

setup: done.

  repo    $WORK
  remote  $ORIGIN
  branch  feature/violations (2 rule violations + 1 filler docs section)
          feature/clean      (compliant)

Before recording:

  cd $WORK
  export PATH="$TARGET/bin:\$PATH"

Then follow the shot list in $SOURCE_DIR/README.md.
EOF
