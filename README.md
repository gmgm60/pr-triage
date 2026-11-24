# PR Triage

**Cut PR review time to the remainder.** A GitHub Action that reads a PR's diff,
works out which files are safe to skip (pure formatting, renames, docs), runs
the test gate and line-level coverage on the rest, and posts one report comment
— plus a browser userscript that marks the pre-cleared files as **Viewed** with
a single click.

On a real 81-file PR, this reduced the human's review surface to **12 lines of
reading + 7 files** — everything else was provably formatting.

---

## What it detects

Every changed file lands in one of nine categories:

| Category | Meaning | Auto "pre-cleared"? |
|---|---|---|
| 🔧 `FORMAT_ONLY` | spacing/re-wrap/`dart format` — token delta 0 | ✅ |
| ⚙️ `MODERNIZED` | formatting + `super.key`, redundant casts, wildcard params, safe import removal | ✅ |
| 🔀 `RENAME` | moved, zero content change | ✅ |
| 📄 `NON_CODE` | pure docs/ignores (`.md`, `.gitignore`, changelog) | ✅ |
| ⚙️ `CONFIG` | behavior-driving config (pubspec, podspec, Package.swift…) | ❌ review |
| 🧪 `TEST_ADDED` | new test file (good — but notice it) | ❌ review |
| 🚨 `TEST_MODIFIED` | existing test file changed — **violation** | ❌ never |
| 👁 `MANUAL_CHECK` | ≤ 5 differing tokens, evidence shown inline | ❌ quick judgment |
| ⚠️ `REAL_CHANGES` | real logic changes | ❌ review |

The classifier is **conservative by design**: anything with real content is
never pre-cleared, and borderline files surface as *quick judgments* with the
exact differing tokens shown (`'1.19.9' → '1.20.1'`) so a human can decide in
seconds.

### Detection technique
Token-level diff normalization that is string-literal-aware: whitespace inside
string literals counts as content, whitespace inside `${...}` interpolation
holes counts as formatting, line re-wrapping and trailing commas normalize
away, and comment-only diffs are ignored. "Modernization" patterns
(`Key? key` → `super.key`, `T(x) : super(x)` → `T(super.x)`, redundant
`as T` casts, `__` → `_`) are only trusted in the non-strict tier.

### Test gate (`full: true`, default)
- Shallow-clones the PR head and auto-detects the runner
  (`flutter test` → `dart test` → `npm test` → `pytest` → `make test`)
- Attributes every failing test: **pre-existing test broken = violation,
  workflow exits red**; tests added by the PR failing = reported separately
- 15-minute timeout; log tail included in the comment and HTML report

### Line-level coverage
- Runs `flutter test --coverage`, parses the LCOV, and checks the **exact added
  lines** of every real-change file: `covered 10/64`, `partial` (uncovered
  lines listed), or `🚨 no coverage`
- Static import-reference fallback for non-LCOV ecosystems

---

## Setup for a repository

1. Copy [`example-workflow.yml`](example-workflow.yml) to
   `.github/workflows/pr-triage.yml` in the repo.
2. That's it. The workflow:

| Trigger | Behavior |
|---|---|
| PR opened / pushed / reopened | runs automatically (full report by default) |
| `/triage` or `/triage full` comment | re-runs on demand (collaborators only, gets a 👍 ack) |
| Actions tab → *Run workflow* | manual run with a PR number |

The report comment is **upserted** — found via a hidden marker, so re-runs
update it in place instead of spamming new comments.

3. For Flutter/Dart repos keep the `subosito/flutter-action` step from the
   example (it provides the SDK for the test gate). Other ecosystems work
   out of the box — remove that step.

Permissions used: `contents: read`, `pull-requests: write` (the default
`GITHUB_TOKEN` — **no secrets, no bot accounts, no PATs anywhere**).

> Public repos get free, unlimited Actions minutes. On private repos, expect
> ~2–4 min per run for a Flutter test gate; set `full: 'false'` to make the
> auto-run fast and use `/triage full` on demand instead.

---

## The userscript (one-click "mark pre-cleared as viewed")

GitHub's *Viewed* checkbox is personal review state — a bot cannot tick it for
you. The userscript bridges that: it reads the file list the bot embedded in
its comment and clicks exactly those Viewed boxes using your own logged-in
session. No tokens, no API access; it can only do what you could do by hand.

**Install — Chrome / Edge / Brave (Tampermonkey):**
1. Install [Tampermonkey](https://www.tampermonkey.net/) from the store.
2. Open [`userscript/mark-viewed.user.js`](userscript/mark-viewed.user.js) →
   *Raw* button → Tampermonkey offers to install. Done.

**Install — Safari (Userscripts app):**
1. Install the free, open-source
   [Userscripts](https://apps.apple.com/app/userscripts/id1463298887) app from
   the Mac App Store and enable it in Safari → Settings → Extensions.
2. Add a new script and paste the contents of
   `userscript/mark-viewed.user.js`.

**Use it:** open any PR's *Files changed* tab → a green
**"✅ Mark pre-cleared as viewed"** button appears bottom-left → click once.
A toast summarizes: marked / already-viewed / left-for-review. If a PR has no
triage comment, the button offers "mark ALL viewed" instead.

> Tip: pair it with the [Refined GitHub](https://github.com/refined-github/refined-github)
> extension (Chrome + Safari), whose `batch-mark-files-as-viewed`
> (shift-click ranges), `dim-viewed-files` and `pr-jump-to-first-non-viewed-file`
> features complement this workflow. Selector strategy in this userscript is
> adapted from Refined GitHub's MIT-licensed code.

---

## The daily workflow

```
PR opens ──► 🤖 triage comment appears automatically
             │
             ├─ ✅ 62 files pre-cleared     (formatting/renames/docs)
             ├─ 👁 5 quick judgments        (tokens shown inline)
             ├─ ⚠️ 7 files need review      (with coverage + links)
             └─ 🧪 tests PASS/fail
             │
reviewer ───► reads ~12 lines
             ├─ clicks "✅ Mark pre-cleared as viewed" (userscript, 1 click)
             └─ reviews only what's left
```

## Report artifacts

Each run writes to the workspace (upload as workflow artifacts if you like):
- `triage-report.md` — the comment body
- `triage-report.html` — full report: every file, diff lines colored
  (yellow = formatting violation, green/red = real added/removed), test-gate
  card, coverage badges
- `triage-result.json` — machine-readable

## Exit codes / outputs

- Action output `verdict`: `ok` or `test-violation`
- The workflow run goes red when a PR **modifies existing test files** or
  **breaks a pre-existing test** — a review-blocking signal, not noise.

## FAQ

**Can the bot tick "Viewed" for me?** No — GitHub's Viewed state is strictly
per-account, and there is no API for marking another user's files. The
userscript is the honest bridge (your session, your clicks, one button).

**What happens when the PR is pushed to again?** The workflow re-runs and
*updates* the same comment. GitHub itself un-views files that changed
("Changed since last view" badge), so stale marks can't hide new changes.

**Does it run on fork PRs?** The analysis runs, but the read-only token on
fork events may not allow comment posting; the report is still in the run's
artifacts. `/triage` commands from collaborators also work on fork PRs.

**Is anything written to my repos?** Only the one comment (updated in place).
The temp clone used for tests is discarded. No tokens are stored anywhere.

**Why is a pubspec.yaml change not pre-cleared?** Dependency bumps drive
behavior (they *caused* this whole workflow once); config files are always
held for review.

## Related

- Local CLI with the same engine, including `--mark` (ticks Viewed via your
  `gh` login) and `--undo`: see the CLI section of
  [the repo this was born from](https://github.com/gmgm60/zego_zimkit_flutter).
- [Refined GitHub](https://github.com/refined-github/refined-github) —
  selector research and complementary review features (MIT).

## License

MIT
