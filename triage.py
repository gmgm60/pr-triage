#!/usr/bin/env python3
"""
PR Triage — GitHub Actions engine.

Classifies a PR's changed files (formatting-only vs real changes), optionally
runs a test gate + line-level coverage in a temp clone, and posts/updates a
markdown report comment on the PR. Report-only: it never touches review state.

Inputs (flags or env):
  --pr N            PR number        (env PR_TRIAGE_PR, or the triggering event)
  --repo owner/name repo             (env GITHUB_REPOSITORY)
  --full            run test gate + coverage (default: on; env PR_TRIAGE_FULL)
  --repo-dir DIR    use this checkout instead of cloning (optional)

Outputs, written to the working directory:
  triage-report.md      the PR comment body (markdown)
  triage-report.html    full colored report (upload as artifact)
  triage-result.json    machine-readable results

Comment identity: github-actions[bot] via GH_TOKEN (env). The comment is
upserted — found via the hidden marker <!-- pr-triage-report -->.
"""

import argparse
import datetime
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

MARKER = "<!-- pr-triage-report -->"
SAFE_MARKER = "pr-triage-safe"
MARKABLE = ("RENAME", "FORMAT_ONLY", "MODERNIZED", "NON_CODE")
MANUAL_RESIDUAL_TOKENS = 5
TEST_GATE_TIMEOUT = 900

STR_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
IMPORT_RE = re.compile(r"""^\s*import\s+['\"].*['\"]\s*;?\s*$""")
LIB_RE = re.compile(r"^\s*library\s+[\w.]*\s*;?\s*$")
WORD_TOK_RE = re.compile(
    r"[A-Za-z_$]\w*|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
TEST_PATH_RE = re.compile(r"(^|/)(test|tests|spec|specs|__tests__)/")
TEST_NAME_RE = re.compile(
    r"(_test\.(dart|go|py)$|\.test\.(js|ts|jsx|tsx|mjs)$|"
    r"^test_[^/]*\.py$|_spec\.(rb|js)$)")
DOC_EXTS = (".md", ".markdown", ".txt", ".arb", ".changes")
DOC_NAMES = ("changelog", "license", "notice", "authors", "contributors",
             ".gitignore", ".gitattributes", "code_of_conduct",
             "contributing.md", "security.md")
CONFIG_NAMES = ("pubspec.yaml", "pubspec.lock", "package.swift",
                "package.resolved", "analysis_options.yaml", ".podspec",
                "cmakelists.txt", "podfile", "cartfile", "build.gradle",
                "gradle.properties", "settings.gradle", "dockerfile")

# ----------------------------------------------------------------------------- utils

def die(msg, code=1):
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(code)

def log(msg):
    print(msg, file=sys.stderr)

def run(cmd, cwd=None, timeout=None, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                          timeout=timeout, env=env)

def run_gh(args):
    return subprocess.run(["gh"] + args, capture_output=True, text=True)

def is_test_file(path):
    return bool(TEST_PATH_RE.search(path) or TEST_NAME_RE.search(path))

def is_config_file(path):
    base = os.path.basename(path).lower()
    ext = os.path.splitext(path)[1].lower()
    full = path.lower()
    if ext == ".yaml" and ("ci" in base or "workflow" in full):
        return True
    if ext in (".podspec", ".xcconfig", ".gradle", ".kts", ".plist", ".toml"):
        return True
    return base in CONFIG_NAMES or base.endswith(".podspec")

def is_doc_file(path):
    base = os.path.basename(path).lower()
    ext = os.path.splitext(path)[1].lower()
    return ext in DOC_EXTS or base in DOC_NAMES or base.startswith(".git")

# ----------------------------------------------------------------------------- github

def resolve_pr_number(args):
    if args.pr:
        return int(args.pr)
    env_pr = os.environ.get("PR_TRIAGE_PR")
    if env_pr and env_pr.isdigit():
        return int(env_pr)
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path) as f:
            event = json.load(f)
        if "pull_request" in event:
            return int(event["pull_request"]["number"])
        if event.get("issue") and "pull_request" in event.get("issue", {}):
            return int(event["issue"]["number"])  # PR comment event
    die("cannot determine PR number (use --pr or PR_TRIAGE_PR)")

def resolve_repo(args):
    if args.repo:
        return tuple(args.repo.split("/"))
    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if gh_repo:
        return tuple(gh_repo.split("/"))
    die("cannot determine repository (use --repo or GITHUB_REPOSITORY)")

def fetch_pr_files(owner, repo, number):
    files, page = [], 1
    while True:
        r = run_gh(["api", f"repos/{owner}/{repo}/pulls/{number}/files",
                    "--method", "GET", "-f", "per_page=100", "-f", f"page={page}"])
        if r.returncode != 0:
            die(f"gh api failed fetching PR files: {r.stderr.strip()[:300]}")
        batch = json.loads(r.stdout)
        files.extend(batch)
        if len(batch) < 100:
            return files
        page += 1

def fetch_pr_info(owner, repo, number):
    r = run_gh(["api", f"repos/{owner}/{repo}/pulls/{number}"])
    if r.returncode != 0:
        die(f"gh api failed fetching PR info: {r.stderr.strip()[:300]}")
    d = json.loads(r.stdout)
    return {
        "head_sha": d.get("head", {}).get("sha"),
        "head_ref": d.get("head", {}).get("ref"),
        "head_clone_url": (d.get("head", {}).get("repo") or {}).get("clone_url"),
        "base_clone_url": (d.get("base", {}).get("repo") or {}).get("clone_url"),
        "title": d.get("title"),
        "author_association": d.get("author_association"),
    }

def check_command_permission():
    """If triggered by a comment command, only collaborators+ may run."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return None
    with open(event_path) as f:
        event = json.load(f)
    comment = event.get("comment")
    if not comment:
        return None
    assoc = comment.get("author_association", "NONE")
    if assoc not in ("OWNER", "MEMBER", "COLLABORATOR"):
        log(f"command by {comment.get('user', {}).get('login')} ({assoc}) — not authorized")
        return comment
    # acknowledge with a reaction (best-effort)
    try:
        run_gh(["api", f"repos/{event['repository']['full_name']}/issues/"
                   f"comments/{comment['id']}/reactions",
                "--method", "POST", "-f", "content=+1"])
    except Exception:
        pass
    return None

# ----------------------------------------------------------------------------- classification (same engine as local CLI)

def split_patch(patch):
    added, removed = [], []
    for line in patch.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed

def is_comment_line(line):
    return line.strip().startswith("//")

def norm_literal(lit):
    lit = re.sub(r"\s+", " ", lit)

    def fix_hole(m):
        h = re.sub(r"\s+", "", m.group(0))
        return re.sub(r",+(?=[)}])", "", h)

    return re.sub(r"\$\{[^}]*\}", fix_hole, lit)

def tokenize(s):
    toks, pos = [], 0
    for m in STR_RE.finditer(s):
        for ch in s[pos:m.start()]:
            if not ch.isspace() and ch != ",":
                toks.append(ch)
        toks.append(norm_literal(m.group(0)))
        pos = m.end()
    for ch in s[pos:]:
        if not ch.isspace() and ch != ",":
            toks.append(ch)
    return toks

def code_join(lines):
    return "".join(l.strip() for l in lines if not is_comment_line(l))

def t1_whitespace(added, removed):
    a = sorted(re.sub(r"\s", "", l) for l in added if not is_comment_line(l))
    r = sorted(re.sub(r"\s", "", l) for l in removed if not is_comment_line(l))
    return a == r

def t2_tokens(added, removed):
    a = tokenize(code_join(added))
    r = tokenize(code_join(removed))
    return a == r or sorted(a) == sorted(r)

def sym_rewrites(s):
    s = s.replace("super.key", "Key? key")
    s = re.sub(r"\)\s*:\s*super\(\s*key:\s*key\s*\)", ")", s)
    s = re.sub(r"\(([^()]*?)\)\s*:\s*super\(\s*\w+\s*\)", "SUPERCTOR", s)
    s = re.sub(r"\(\s*super\.\w+\s*\)(?!\()", "SUPERCTOR", s)
    s = re.sub(r"\(([^()]*?)\s+as\s+[A-Za-z_][\w.<>?\[\]]*\)", r"\1", s)
    s = re.sub(r"\bas\s+[A-Za-z_][\w.<>?\[\]]*", "", s)
    s = re.sub(r"\b__\b", "_", s)
    return s

def drop_removed_noops(removed, added):
    added_set = {l.strip() for l in added}
    keep = []
    for l in removed:
        ls = l.strip()
        if IMPORT_RE.match(l) and ls not in added_set:
            continue
        if LIB_RE.match(l) and ls not in added_set:
            continue
        keep.append(l)
    return keep

def t3_modernized(added, removed):
    r2 = drop_removed_noops(removed, added)
    a = tokenize(sym_rewrites(code_join(added)))
    r = tokenize(sym_rewrites(code_join(r2)))
    return a == r or sorted(a) == sorted(r)

def differing_tokens(added, removed, limit=6):
    r2 = drop_removed_noops(removed, added)

    def words(lines):
        toks = WORD_TOK_RE.findall(sym_rewrites(code_join(lines)))
        return [norm_literal(t) if t[:1] in "'\"" else t for t in toks]

    from collections import Counter
    ca, cr = Counter(words(added)), Counter(words(r2))
    only_a = list((ca - cr).elements())
    only_r = list((cr - ca).elements())
    return only_a[:limit], only_r[:limit], len(only_a) + len(only_r)

def classify_file(f, strict=False):
    path, status = f["filename"], f.get("status", "modified")
    adds, dels = f.get("additions", 0), f.get("deletions", 0)
    patch = f.get("patch")

    res = {"path": path, "status": status, "additions": adds, "deletions": dels,
           "category": None, "residual": 0, "evidence": ""}

    if status == "renamed" and adds == 0 and dels == 0:
        res["category"] = "RENAME"
        res["evidence"] = "renamed, 0 content change"
        return res
    if patch is None:
        res["category"] = "REAL_CHANGES"
        res["evidence"] = "binary/no-diff — needs review"
        return res
    if is_test_file(path):
        if status == "added":
            res["category"] = "TEST_ADDED"
            res["evidence"] = f"new test file (+{adds})"
        else:
            res["category"] = "TEST_MODIFIED"
            res["evidence"] = "existing test file changed — verify why"
        return res
    if is_config_file(path):
        res["category"] = "CONFIG"
        res["evidence"] = "behavior-driving config — review changes"
        return res
    if is_doc_file(path):
        res["category"] = "NON_CODE"
        res["evidence"] = "docs/ignore file"
        return res

    added, removed = split_patch(patch)
    if not any(not is_comment_line(l) for l in added + removed):
        res["category"] = "FORMAT_ONLY"
        res["evidence"] = "comment/blank-line changes only"
        return res
    if t1_whitespace(added, removed) or t2_tokens(added, removed):
        res["category"] = "FORMAT_ONLY"
        res["evidence"] = "token delta 0 (whitespace/wrap only)"
        return res
    if not strict and t3_modernized(added, removed):
        res["category"] = "MODERNIZED"
        res["evidence"] = "formatting + modernization patterns only"
        return res

    only_a, only_r, residual = differing_tokens(added, removed)
    res["residual"] = residual
    if residual <= MANUAL_RESIDUAL_TOKENS:
        res["category"] = "MANUAL_CHECK"
        ev = " ⟷ ".join(t for t in only_r[:3])
        ev2 = " ⟷ ".join(t for t in only_a[:3])
        res["evidence"] = f"tokens: {ev or '∅'} → {ev2 or '∅'}"
    else:
        res["category"] = "REAL_CHANGES"
        res["evidence"] = f"{residual} differing tokens"
    return res

# ----------------------------------------------------------------------------- test gate + coverage

def detect_runner(repo_dir):
    pubspec = os.path.join(repo_dir, "pubspec.yaml")
    if os.path.exists(pubspec):
        text = open(pubspec, encoding="utf-8", errors="replace").read()
        if re.search(r"^\s*flutter\s*:", text, re.M) or "sdk: flutter" in text:
            return ["flutter", "test"]
        return ["dart", "test"]
    pkg = os.path.join(repo_dir, "package.json")
    if os.path.exists(pkg):
        try:
            d = json.load(open(pkg))
            t = (d.get("scripts") or {}).get("test", "")
            if t and "no test specified" not in t:
                return ["npm", "test"]
        except json.JSONDecodeError:
            pass
    if any(os.path.exists(os.path.join(repo_dir, p)) for p in
           ("pyproject.toml", "setup.py", "requirements.txt")):
        return ["python3", "-m", "pytest"]
    mk = os.path.join(repo_dir, "Makefile")
    if os.path.exists(mk) and re.search(r"^test:", open(mk).read(), re.M):
        return ["make", "test"]
    return None

def parse_failing_tests(runner, output):
    failing = set()
    for line in output.splitlines():
        if "[E]" in line or line.startswith("FAILED") or " FAIL " in line:
            for m in re.finditer(r"[\w./\\-]*_test\.(dart|py|go)|"
                                 r"[\w./\\-]*\.test\.[a-z]+|"
                                 r"[\w./\\-]*_spec\.[a-z]+", line):
                failing.add(m.group(0).lstrip("./"))
    return sorted(failing)

def run_gates(owner, repo, number, pr_files, want_coverage, repo_dir=None):
    gate = {"ran": True, "verdict": None, "runner": None, "failures": [],
            "broke_existing": False, "coverage_available": False,
            "coverage": {}, "log_tail": "", "error": None, "clone": None}
    tmp = None
    try:
        if repo_dir and os.path.isdir(repo_dir):
            dest = repo_dir
            gate["clone"] = "provided"
        else:
            info = fetch_pr_info(owner, repo, number)
            tmp = tempfile.mkdtemp(prefix="pr-triage-")
            dest = os.path.join(tmp, "repo")
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            clone_url = info.get("head_clone_url") or info.get("base_clone_url") or \
                f"https://github.com/{owner}/{repo}.git"
            r = run(["gh", "repo", "clone", clone_url, dest, "--", "--depth", "1",
                     "--branch", info.get("head_ref") or "HEAD"], env=env, timeout=300)
            if r.returncode != 0:
                r = run(["gh", "repo", "clone", f"{owner}/{repo}", dest, "--",
                         "--depth", "1"], env=env, timeout=300)
                if r.returncode != 0:
                    gate["error"] = "clone failed: " + (r.stderr or "").strip()[:200]
                    return gate
                rf = run(["git", "-C", dest, "fetch", "--depth", "1", "origin",
                          f"refs/pull/{number}/head"], env=env, timeout=300)
                if rf.returncode == 0:
                    run(["git", "-C", dest, "checkout", "FETCH_HEAD"],
                        env=env, timeout=120)
            gate["clone"] = "temp"

        runner = detect_runner(dest)
        gate["runner"] = runner
        if runner is None:
            gate["verdict"] = "no-test-runner"
            gate["log_tail"] = "no recognized test runner in repo"
            _attach_coverage(gate, dest, pr_files, want_coverage)
            return gate

        cmd = list(runner)
        if want_coverage and runner == ["flutter", "test"]:
            cmd.append("--coverage")
        elif want_coverage and runner == ["dart", "test"]:
            cmd.append("--coverage=coverage")
        try:
            t = run(cmd, cwd=dest, timeout=TEST_GATE_TIMEOUT)
            out = (t.stdout or "") + (t.stderr or "")
            rc = t.returncode
        except subprocess.TimeoutExpired:
            gate["verdict"] = "timeout"
            gate["log_tail"] = f"test run exceeded {TEST_GATE_TIMEOUT}s"
            _attach_coverage(gate, dest, pr_files, want_coverage)
            return gate

        gate["log_tail"] = "\n".join(out.splitlines()[-40:])
        failing = parse_failing_tests(runner, out)
        changed = {f["filename"]: f.get("status") for f in pr_files}
        for tf in failing:
            st = changed.get(tf) or changed.get("/" + tf)
            if st:
                gate["failures"].append({"test": tf,
                                         "attribution": "PR test failing (touched by PR)"})
            else:
                gate["failures"].append({"test": tf,
                                         "attribution": "PRE-EXISTING TEST BROKEN BY PR"})
                gate["broke_existing"] = True
        if rc == 0:
            gate["verdict"] = "pass"
        elif failing:
            gate["verdict"] = "fail"
        else:
            gate["verdict"] = "fail-unparsed"
            gate["broke_existing"] = True  # conservative
        _attach_coverage(gate, dest, pr_files, want_coverage)
        return gate
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

def parse_lcov(path):
    cov = {}
    cur = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return cov
    with fh:
        for line in fh:
            if line.startswith("SF:"):
                cur = line[3:].strip()
                cov.setdefault(cur, {})
            elif line.startswith("DA:") and cur is not None:
                parts = line[3:].strip().split(",")
                try:
                    cov[cur][int(parts[0])] = int(parts[1])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("end_of_record"):
                cur = None
    return cov

def added_line_numbers(patch):
    res, newln = [], 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            newln = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            res.append(newln)
            newln += 1
        elif line.startswith("-") and not line.startswith("---"):
            pass
        else:
            newln += 1
    return res

def _attach_coverage(gate, dest, pr_files, want_coverage):
    if not want_coverage:
        return
    lcov_path = os.path.join(dest, "coverage", "lcov.info")
    lcov = parse_lcov(lcov_path) if os.path.exists(lcov_path) else {}
    if lcov:
        gate["coverage_available"] = True
        for f in pr_files:
            patch = f.get("patch") or ""
            if not patch:
                continue
            added = added_line_numbers(patch)
            match = None
            for sf in lcov:
                if sf.replace("\\", "/").endswith(f["filename"].replace("\\", "/")):
                    match = sf
                    break
            if match is None:
                gate["coverage"][f["filename"]] = {"verdict": "NO COVERAGE"}
                continue
            da = lcov[match]
            executable = [ln for ln in added if ln in da]
            covered = [ln for ln in executable if da[ln] > 0]
            uncovered = [ln for ln in executable if da[ln] == 0]
            if not executable:
                gate["coverage"][f["filename"]] = {
                    "verdict": "n/a (no executable added lines)"}
            elif not uncovered:
                gate["coverage"][f["filename"]] = {
                    "verdict": "covered", "covered": len(covered),
                    "executable": len(executable)}
            elif covered:
                gate["coverage"][f["filename"]] = {
                    "verdict": "partial", "covered": len(covered),
                    "executable": len(executable), "uncovered": uncovered[:8]}
            else:
                gate["coverage"][f["filename"]] = {
                    "verdict": "NO COVERAGE", "covered": 0,
                    "executable": len(executable), "uncovered": uncovered[:8]}
    else:
        test_blobs = []
        for root, _dirs, fs in os.walk(dest):
            if "/.git" in root:
                continue
            for fn in fs:
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, dest)
                if is_test_file(rel) and os.path.getsize(p) < 500_000:
                    try:
                        test_blobs.append((rel, open(p, encoding="utf-8",
                                                     errors="replace").read()))
                    except OSError:
                        pass
        for f in pr_files:
            rel = f["filename"]
            stem = os.path.splitext(os.path.basename(rel))[0]
            refs = [t for t, txt in test_blobs
                    if rel in txt or f"/{stem}.dart" in txt
                    or (stem in txt and stem not in ("main", "test"))]
            gate["coverage"][rel] = ({"verdict": "covered (static)",
                                      "by": refs[:4]} if refs
                                     else {"verdict": "NO COVERAGE (static)"})
        gate["coverage_available"] = True

# ----------------------------------------------------------------------------- reports

CATEGORY_ORDER = ["TEST_MODIFIED", "REAL_CHANGES", "MANUAL_CHECK", "TEST_ADDED",
                  "CONFIG", "FORMAT_ONLY", "MODERNIZED", "RENAME", "NON_CODE"]

def blob_url(owner, repo, sha, path):
    return f"https://github.com/{owner}/{repo}/blob/{sha}/{path}" if sha else ""

def md_report(results, owner, repo, number, head_sha, gate):
    esc = lambda s: s.replace("|", "\\|").replace("`", "`\u200b")
    by = {c: [r for r in results if r["category"] == c] for c in CATEGORY_ORDER}
    markable = sum(len(by[c]) for c in MARKABLE)
    safe_paths = sorted(r["path"] for r in results if r["category"] in MARKABLE)

    lines = []
    lines.append("## 🤖 PR Triage")
    lines.append("")
    lines.append(f"**{markable} of {len(results)} files pre-cleared** "
                 f"(formatting / renames / docs) · **{len(by['MANUAL_CHECK'])} quick"
                 f" judgments** · **{len(by['REAL_CHANGES'])} need review** · "
                 f"**{len(by['TEST_MODIFIED'])} test violations**")
    lines.append("")

    if gate and gate.get("ran"):
        v = gate["verdict"]
        icon = {"pass": "✅", "no-test-runner": "ℹ️", "timeout": "⏱️"}.get(v, "❌")
        runner = " ".join(gate["runner"] or ["?"])
        lines.append(f"**{icon} Test gate** (`{runner}`): **{v}**")
        for fail in gate["failures"]:
            lines.append(f"- `{esc(fail['test'])}` — {fail['attribution']}")
        lines.append("")

    if by["TEST_MODIFIED"]:
        lines.append("### 🚨 Existing test files modified")
        lines.append("")
        for r in sorted(by["TEST_MODIFIED"], key=lambda x: x["path"]):
            lines.append(f"- `{r['path']}` — {r['evidence']}")
        lines.append("")

    if by["MANUAL_CHECK"]:
        lines.append("### 👁 Quick judgments (tiny changes — glance & decide)")
        lines.append("")
        lines.append("| File | What actually differs |")
        lines.append("|---|---|")
        for r in sorted(by["MANUAL_CHECK"], key=lambda x: x["path"]):
            lines.append(f"| `{esc(r['path'])}` | {esc(r['evidence'])} |")
        lines.append("")

    if by["REAL_CHANGES"]:
        lines.append("### ⚠️ Review needed")
        lines.append("")
        lines.append("| File | Δ | Coverage |")
        lines.append("|---|---|---|")
        for r in sorted(by["REAL_CHANGES"], key=lambda x: x["path"]):
            cov = ""
            if gate and r["path"] in gate.get("coverage", {}):
                c = gate["coverage"][r["path"]]
                if c["verdict"].startswith("NO COVERAGE"):
                    cov = "🚨 no coverage"
                elif c["verdict"] == "covered":
                    cov = f"✅ {c['covered']}/{c['executable']}"
                elif c["verdict"] == "partial":
                    cov = f"⚠️ {c['covered']}/{c['executable']}"
                else:
                    cov = c["verdict"]
            link = f"[`{esc(os.path.basename(r['path']))}`]({blob_url(owner, repo, head_sha, r['path'])})"
            lines.append(f"| {link} | +{r['additions']} −{r['deletions']} | {cov} |")
        lines.append("")

    extras = [(c, len(by[c])) for c in ("TEST_ADDED", "CONFIG") if by[c]]
    if extras:
        lines.append("ℹ️ " + " · ".join(f"**{c}**: {n}" for c, n in extras))
        lines.append("")

    lines.append(f"<details><summary>✅ {markable} pre-cleared files — safe to "
                 f"mark as Viewed (userscript reads this list)</summary>")
    lines.append("")
    lines.append("\n".join(f"- `{p}`" for p in safe_paths))
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append(f"<sub>Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M UTC')} · "
                 f"rerun anytime with a <code>/triage</code> comment</sub>")
    lines.append("")
    lines.append(f"<!-- {SAFE_MARKER}: {json.dumps(safe_paths)} -->")
    lines.append(MARKER)
    return "\n".join(lines)

def write_html_report(results, patches, owner, repo, number, path, head_sha, gate):
    esc = html_mod.escape
    css = """
    body { font-family: -apple-system, sans-serif; margin: 2rem; background: #fafafa; }
    h1 { font-size: 1.3rem; }
    .ws   { background: #fff3bf; color: #1f2328; }
    .real-add { background: #d3f9d8; color: #1f2328; }
    .real-del { background: #ffd6d6; color: #1f2328; }
    .cmt  { background: #eaeef2; color: #57606a; }
    .meta { color: #57606a; }
    .ws, .real-add, .real-del, .cmt { display: inline-block; min-width: 100%; }
    .sg-add { color: #116329; font-weight: 700; }
    .sg-del { color: #cf222e; font-weight: 700; }
    details { margin: .6rem 0; border: 1px solid #dee2e6; border-radius: 6px;
              background: #fff; }
    summary { padding: .5rem .8rem; cursor: pointer; display: flex; gap: .8rem;
              align-items: center; flex-wrap: wrap; }
    .badge { font-size: .72rem; font-weight: 600; padding: .15rem .5rem;
             border-radius: 999px; color: #fff; }
    pre { margin: 0; padding: .8rem; overflow-x: auto; font-size: .8rem;
          line-height: 1.45; background: #f6f8fa; color: #24292f;
          border-radius: 0 0 6px 6px; }
    .counts { color: #495057; font-size: .8rem; }
    a { color: #0969da; text-decoration: none; }
    .gate { border-left: 4px solid #94d82d; padding: .6rem .9rem; background: #fff;
            border-radius: 6px; margin: .6rem 0; }
    .gate.bad { border-left-color: #fa5252; }
    .gate ul { margin: .3rem 0 0 1.2rem; }
    """
    badge_colors = {"TEST_MODIFIED": "#c92a2a", "REAL_CHANGES": "#fa5252",
                    "MANUAL_CHECK": "#e8590c", "TEST_ADDED": "#0c8599",
                    "CONFIG": "#f08c00", "FORMAT_ONLY": "#f59f00",
                    "MODERNIZED": "#94d82d", "RENAME": "#22b8cf",
                    "NON_CODE": "#4c6ef5"}
    gate_html = ""
    if gate and gate.get("ran"):
        verdict = gate["verdict"] or "error"
        cls = "gate" if verdict in ("pass", "no-test-runner") else "gate bad"
        fails = "".join(
            f"<li><code>{esc(f['test'])}</code> — {esc(f['attribution'])}</li>"
            for f in gate["failures"])
        gate_html = (f'<div class="{cls}"><b>Test gate</b> '
                     f"({' '.join(gate['runner'] or ['?'])}): {esc(verdict)}"
                     + (f"<ul>{fails}</ul>" if fails else "")
                     + f"<pre>{esc(gate.get('log_tail', '') or '')}</pre></div>")
    rows = []
    for cat in CATEGORY_ORDER:
        for r in sorted((x for x in results if x["category"] == cat),
                        key=lambda x: x["path"]):
            parts = []
            for line in (patches.get(r["path"], "") or "").split("\n"):
                if line[:1] in ("+", "-") and not line.startswith(("+++", "---")):
                    sign, body = line[0], esc(line[1:]) or "&nbsp;"
                    sg = "sg-add" if sign == "+" else "sg-del"
                    hcls = "ws" if (line[1:].strip().startswith("//") or
                                    not line[1:].strip()) else (
                        f"real-{ 'add' if sign == '+' else 'del' }")
                    parts.append(f'<span class="{hcls}">'
                                 f'<span class="{sg}">{sign}</span>{body}</span>\n')
                else:
                    parts.append(f'<span class="meta">{esc(line)}</span>\n')
            cov_html = ""
            if gate and r["path"] in gate.get("coverage", {}):
                cov_html = f" · 🧪 {esc(gate['coverage'][r['path']]['verdict'])}"
            url = blob_url(owner, repo, head_sha, r["path"])
            rows.append(f"""<details><summary>
<span class="badge" style="background:{badge_colors[cat]}">{cat}</span>
<code>{esc(r['path'])}</code>
<span class="counts">+{r['additions']} −{r['deletions']}{cov_html}</span>
</summary><pre>{''.join(parts)}</pre></details>""")
    counts = {c: sum(1 for r in results if r["category"] == c)
              for c in CATEGORY_ORDER}
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>PR triage {esc(owner)}/{esc(repo)}#{number}</title><style>{css}</style></head><body>
<h1>PR Triage — {esc(owner)}/{esc(repo)}#{number}</h1>
<p>{len(results)} files · {sum(counts[c] for c in MARKABLE)} pre-cleared ·
{counts['MANUAL_CHECK']} quick judgments · {counts['REAL_CHANGES']} review</p>
{gate_html}
{''.join(rows)}
</body></html>"""
    with open(path, "w") as f:
        f.write(doc)

# ----------------------------------------------------------------------------- comment upsert

def post_comment(owner, repo, number, body):
    marker_comment = None
    page = 1
    while True:
        r = run_gh(["api", f"repos/{owner}/{repo}/issues/{number}/comments",
                    "--method", "GET", "-f", "per_page=100", "-f", f"page={page}"])
        if r.returncode != 0:
            log(f"could not list comments: {r.stderr.strip()[:200]}")
            return None
        batch = json.loads(r.stdout)
        for c in batch:
            if MARKER in (c.get("body") or ""):
                marker_comment = c
                break
        if marker_comment or len(batch) < 100:
            break
        page += 1

    if marker_comment:
        r = run_gh(["api", f"repos/{owner}/{repo}/issues/comments/"
                     f"{marker_comment['id']}", "--method", "PATCH",
                   "-f", f"body={body}"])
        if r.returncode != 0:
            log(f"could not update comment: {r.stderr.strip()[:200]}")
            return None
        return r.stdout and json.loads(r.stdout).get("html_url")
    r = run_gh(["api", f"repos/{owner}/{repo}/issues/{number}/comments",
                "--method", "POST", "-f", f"body={body}"])
    if r.returncode != 0:
        log(f"could not post comment: {r.stderr.strip()[:200]}")
        return None
    return json.loads(r.stdout).get("html_url")

# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="triage")
    ap.add_argument("--pr", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--full", action="store_true",
                    default=os.environ.get("PR_TRIAGE_FULL", "true").lower()
                            not in ("0", "false", "no"))
    ap.add_argument("--repo-dir", default="")
    ap.add_argument("--no-comment", action="store_true",
                    help="generate reports but do not post/update the PR comment")
    args = ap.parse_args()

    denied = check_command_permission()
    owner, repo = resolve_repo(args)
    number = resolve_pr_number(args)
    if denied:
        post_comment(owner, repo, number,
                     "🤖 PR Triage: not authorized to run /triage "
                     "(collaborators only)." + "\n" + MARKER)
        sys.exit(0)

    log(f"triaging {owner}/{repo}#{number}")
    files = fetch_pr_files(owner, repo, number)
    if not files:
        die("PR has no changed files")
    info = fetch_pr_info(owner, repo, number)
    head_sha = info.get("head_sha")

    gate = None
    if args.full:
        if not shutil.which("git"):
            die("git not found")
        log("running gates (clone + tests + coverage) …")
        gate = run_gates(owner, repo, number, files, want_coverage=True,
                         repo_dir=args.repo_dir or None)

    patches = {f["filename"]: f.get("patch") or "" for f in files}
    results = [classify_file(f) for f in files]
    if gate and gate.get("coverage"):
        for r in results:
            c = gate["coverage"].get(r["path"])
            if c:
                r["coverage"] = c
    results.sort(key=lambda r: (CATEGORY_ORDER.index(r["category"]), r["path"]))

    md = md_report(results, owner, repo, number, head_sha, gate)
    with open("triage-report.md", "w") as f:
        f.write(md)
    write_html_report(results, patches, owner, repo, number,
                      "triage-report.html", head_sha, gate)
    with open("triage-result.json", "w") as f:
        json.dump({"owner": owner, "repo": repo, "number": number,
                   "head_sha": head_sha, "gate": gate, "results": results}, f, indent=2)

    url = None if args.no_comment else post_comment(owner, repo, number, md)
    markable = sum(1 for r in results if r["category"] in MARKABLE)
    violations = [r for r in results if r["category"] == "TEST_MODIFIED"]
    print(f"::notice::PR triage: {markable}/{len(results)} pre-cleared · "
          f"{len(violations)} test violations"
          + (f" · gate {gate['verdict']}" if gate and gate.get("verdict") else ""))
    if url:
        log(f"comment: {url}")
    else:
        log("WARNING: comment not posted (permissions?) — artifacts still written")
    if violations or (gate and gate.get("broke_existing")):
        sys.exit(2)

if __name__ == "__main__":
    main()
