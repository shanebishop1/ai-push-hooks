# Demo: recording ai-push-hooks

A throwaway repo, six short clips, no audio. Every beat has to be readable on
screen, so the demo is built out of title cards, typed commands, and real hook
output. Nothing here is faked: the runs are live, and only idle time is
compressed when the `.cast` is converted to a GIF.

## What the demo repo contains

`orders-service`, a small repo with an `AGENTS.md` holding three house rules:

1. **Use the typed API client** — components never call `fetch()` directly.
2. **Migrations stay backward-compatible** — no destructive change during a rolling deploy.
3. **No marketing filler in docs** — no unverifiable claims.

Two branches are prepared:

| Branch | Contents |
| --- | --- |
| `feature/violations` | A refund panel calling `fetch()` directly, a migration that `DROP COLUMN`s a column the current release still reads, and a README paragraph of superlatives. |
| `feature/clean` | An invoice card using the typed client, and an additive migration. |

Rule 1 has an exact textual signature, so `tests/check_rules.py` greps for it.
Rules 2 and 3 have no regex — which is the point of the review step, and worth
letting a viewer notice on their own.

## Setup

```bash
examples/demo/setup.sh            # builds ~/aph-demo
cd ~/aph-demo/work
export PATH="$HOME/aph-demo/bin:$PATH"
```

This creates the repo, a bare remote (so `git push` genuinely fires the hook),
installs the pre-push hook, and gives you three short commands to use on camera:

- `use act1|act2|act3|act4|extra` — swap in that act's workflow and commit the swap
- `reset` — rebuild `feature/violations` and clear artifacts, to re-record an act
- `card "Title" "subtitle"` — a full-screen title card, standing in for narration

Authenticate `opencode` (and `claude` for act 4) before recording.

## Recording

```bash
pipx install asciinema
cargo install --locked agg      # or grab an agg release binary

asciinema rec --idle-time-limit=1.5 --cols=100 --rows=28 act1.cast
agg --font-size 20 --theme asciinema act1.cast act1.gif
```

`--idle-time-limit=1.5` is what makes this work as a GIF: a real 40-second model
call collapses to a 1.5-second pause, and nothing on screen is altered. Record
each act as its own file — a handful of 20-40s GIFs beat one long video that
nobody scrubs through.

Set `CARD_SECONDS=3` if the title cards feel too fast to read on playback.

---

## Act 1 — It blocks a bad push (~30s)

The hero clip. If only one GIF ships, ship this one.

```bash
card "AGENTS.md tells your agent how to work." "Nothing checks whether it did."
cat AGENTS.md
git log --oneline -1
git push -u origin feature/violations
```

The push stops. The findings name the file and the rule each change broke.
Let that sit on screen for a beat, then reveal how short the config is:

```bash
card "Three steps. Twenty-five lines."
cat ai-push-hooks.toml
```

**What a viewer should catch:** `ask` reports; `assert` blocks. The gate is
something you wrote, not a default the tool assumed.

## Act 2 — It fixes what it found (~40s)

```bash
card "Now let it fix them." "One step. A path allowlist."
use act2
git diff HEAD~1 -- ai-push-hooks.toml     # shows just the new apply step
git push origin feature/violations
```

The push still stops — but now the working tree has changed:

```bash
card "It edited. It did not commit."
git status --short
git diff
```

**What a viewer should catch:** `allow_paths` bounds the blast radius, the edits
landed in the working tree, and the commit is still yours to make. Let the
`git status` frame linger; that's the trust moment.

This is the default. Two opt-ins move further along the spectrum, and Act 6
shows the far end.

Finish the loop on camera — it's satisfying and it proves the fix is real:

```bash
git commit -aqm "refunds: use typed client, keep migration additive, cut filler"
git push origin feature/violations
```

## Act 3 — It proves the fix landed (~35s)

```bash
reset
card "A runner exiting 0 is not evidence." "Check the result yourself."
use act3
git diff HEAD~1 -- ai-push-hooks.toml     # shows just the new exec step
git push origin feature/violations
```

The project's own `tests/check_rules.py` runs against the real checkout after
the edits propagate, and a nonzero exit blocks the push.

**What a viewer should catch:** AI review composes with the checks you already
trust, rather than replacing them.

## Act 4 — Two models, one workflow (~20s)

```bash
reset
card "Review with one model. Fix with another."
use act4
git diff HEAD~1 -- ai-push-hooks.toml     # a runner profile and one word
git push origin feature/violations
```

OpenCode reviews; Claude Code applies the fix. Keep this clip short — the whole
point is how little config it took.

## Act 5 — Or let it do the whole thing (~20s)

The counterweight to Act 2. Same workflow, two added lines.

```bash
reset
card "Or hand it the keys." "auto_commit + auto_push"
use extra
git diff HEAD~1 -- ai-push-hooks.toml     # two lines
git push origin feature/violations
```

One push. It reviews, fixes, runs your tests, commits with its own message, and
sends it:

```
  push 1  fb03764fe6c2  FAILED     scoped to the pre-fix commit, superseded
  push 2  f85b7cf2dad2  SUCCEEDED  pushed to origin refs/heads/feature/violations
```

**What a viewer should catch:** the spectrum is the point — uncommitted by
default, commit for you, or commit and push — and you choose per step, not per
tool.

**Do not skip the explanation of push 1.** Git scoped your push to the pre-fix
commit before the hook ran, so it cannot carry the fix. Git prints `error: failed
to push some refs` for it afterwards. If you cut that frame, the clip looks like
the tool failed.

## Act 6 — The honest close (~25s)

Short, and worth more than any feature clip to a technical audience.

```bash
card "It gets out of the way when nothing is wrong."
git checkout feature/clean
git push -u origin feature/clean          # review returns [], apply is skipped, push proceeds
```

```bash
card "And it can be bypassed." "Local hooks are fast feedback, not enforcement."
git checkout feature/violations
AI_PUSH_HOOKS_SKIP=1 git push origin feature/violations
```

```bash
card "Every decision is on disk."
ls .git/ai-push-hooks/
cat .git/ai-push-hooks/**/review/issues.json
```

**What a viewer should catch:** the skip works by design, keep CI for real
enforcement, and nothing about the run is hidden — inputs, outputs, logs, and
the transcript are all inspectable files.

---

## Notes for the edit

- **Lead with Act 1's block.** The first three seconds decide whether anyone
  watches the rest; a red blocked push is the strongest opener you have.
- **The `git status` frame in Act 2 is the most important still.** Give it more
  time than feels necessary.
- Trim the `cat ai-push-hooks.toml` shots if the config scrolls past a screen —
  a `git diff` of just the added step reads far better than a full file dump.
- Model output varies run to run. Record each act a few times and keep the take
  where the findings are crisply worded; that is editing, not fabrication.
- If a run produces a false positive, consider keeping it and captioning it.
  A demo that shows the tool being wrong and still useful is more persuasive
  than four flawless takes.

## Cleanup

```bash
rm -rf ~/aph-demo
```
