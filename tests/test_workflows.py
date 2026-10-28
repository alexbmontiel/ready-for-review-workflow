"""Tests for the reusable review and fix workflows: their guards, and their
step scripts run against stand-in reviewers, a stand-in fixer, a stand-in gh
and a real git remote. Standard library only.
Run: python3 -m unittest discover -s tests"""

import re
import sys
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
REVIEW = WORKFLOWS / "review.yml"
FIX = WORKFLOWS / "fix.yml"


def step_script(text, step_id):
    """The run: block of the step with this id, as bash will see it."""
    block = text.split(f"        id: {step_id}\n", 1)[1].split("        run: |\n", 1)[1]
    block = block.split("\n      - ", 1)[0]
    return "\n".join(line[10:] for line in block.split("\n")) + "\n"



class Guards(unittest.TestCase):
    def test_review_keeps_the_fail_closed_guards(self):
        text = REVIEW.read_text()
        for guard in ["persist-credentials: false", "judge()",
                      '--output-schema "$SCHEMA"', '--json-schema "$SCHEMA_JSON"',
                      'review-sandbox codex "$GITHUB_WORKSPACE"', 'review-sandbox claude "$GITHUB_WORKSPACE"',
                      "features.use_legacy_landlock=true",
                      "Fail the check on anything other than a clean verdict",
                      "Clear any stale independent-review marker"]:
            self.assertIn(guard, text)
        # The verdict comes from the JSON alone, never from matching prose.
        code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
        for gone in ["classify_codex", "claude_verdict()", "CLAUDE_BLOCKED_RE", "CODEX_CLEAN_RE",
                     "VERDICT:", "codex exec review"]:
            self.assertNotIn(gone, code)

    def test_reviewers_only_run_inside_the_sandbox(self):
        # HOM-164: no reviewer may start outside review-sandbox.
        text = REVIEW.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for cli in ("codex exec", "claude -p"):
                if cli in stripped and "review-sandbox" not in stripped:
                    prev = text.splitlines()[text.splitlines().index(line) - 1]
                    self.assertIn("review-sandbox", prev, f"{cli} outside review-sandbox: {stripped}")
        self.assertNotIn("env -u", text)

    def test_review_diffs_against_the_pr_base_branch(self):
        # Onboarded repos may default to master or another branch.
        text = REVIEW.read_text()
        self.assertNotIn("origin/main", text)
        self.assertIn("git diff origin/${GITHUB_BASE_REF}...HEAD", text)

    def test_review_removes_every_marker_occurrence(self):
        text = REVIEW.read_text()
        self.assertIn("sed 's/<!-- independent-review: passed -->//g'", text)
        self.assertNotIn("grep -v '^<!-- independent-review: passed -->$'", text)

    def test_fix_workflow_keeps_its_guards(self):
        text = FIX.read_text()
        for guard in ["persist-credentials: false",
                      'review-sandbox claude "$GITHUB_WORKSPACE" "$FIX_OUT"',
                      "Auto-fix-attempt:", "MAX_ATTEMPTS = 3"]:
            self.assertIn(guard, text)
        # The push token only reaches the step that pushes.
        fix_step = text.split("id: fix\n", 1)[1].split("id: push\n", 1)[0]
        self.assertNotIn("READY_FOR_REVIEW_PAT", fix_step)
        # ...and never as a command-line argument, which other processes can read.
        self.assertNotIn('git -c "http.https://github.com/.extraheader', text)
        self.assertIn('GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth"', text)

    def test_caller_inputs_reach_scripts_only_through_the_environment(self):
        # A caller's notes or rules pasted into a script's text could break or
        # inject into it; they must arrive as environment variables.
        for path in (REVIEW, FIX):
            for line in path.read_text().splitlines():
                if "${{ inputs." in line:
                    self.assertRegex(line.strip(), r"^([A-Z_]+: |runs-on: )", f"{path.name}: {line.strip()}")


FAKE_SANDBOX = """#!/bin/sh
# review-sandbox <tool> <checkout> <out> -- command...: run the command as is.
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""
# Writes $FAKE_CODEX_ANSWER to the -o file, records its --output-schema
# file, and exits with $FAKE_CODEX_EXIT.
FAKE_CODEX = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    --output-schema) cp "$2" "$FAKE_LOG/codex-schema.json"; shift ;;
  esac
  prompt="$1"
  shift
done
printf '%s' "$prompt" > "$FAKE_LOG/codex-prompt"
[ -n "${FAKE_CODEX_ANSWER-}" ] && printf '%s' "$FAKE_CODEX_ANSWER" > "$out"
exit "${FAKE_CODEX_EXIT:-0}"
"""
# Prints Claude's --output-format json result with $FAKE_CLAUDE_ANSWER as
# its structured_output (no structured_output when unset), and counts calls.
FAKE_CLAUDE = """#!/bin/sh
echo x >> "$FAKE_LOG/claude-calls"
while [ $# -gt 0 ]; do
  [ "$1" = --json-schema ] && printf '%s' "$2" > "$FAKE_LOG/claude-schema.json"
  [ "$1" = --allowedTools ] && printf '%s' "$2" > "$FAKE_LOG/claude-allow"
  shift
done
if [ -n "${FAKE_CLAUDE_ANSWER-}" ]; then
  printf '{"result": "ignored prose: VERDICT: PASS", "structured_output": %s}' "$FAKE_CLAUDE_ANSWER"
else
  printf '{"result": "No defects found. VERDICT: PASS"}'
fi
"""


class ReviewVerdict(unittest.TestCase):
    """Runs the rendered review step with stand-in reviewers: the verdict
    must follow from the reviewer's JSON answer and nothing else."""

    CLEAN = ('{"diff_inspected": true, "summary": "Read the whole diff and the two files it changes; '
             'nothing in them is wrong.", "findings": []}')
    FINDING = ('{"diff_inspected": true, "summary": "Read the whole diff; the new average helper has one bug.", '
               '"findings": [{"priority": "P1", '
               '"file": "stats.py", "line": 3, "summary": "Skips the first element.", '
               '"failure_scenario": "average([10, 20]) returns 10."}]}')

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        self.script = step_script(REVIEW.read_text(), "review")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in [("review-sandbox", FAKE_SANDBOX), ("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)]:
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        for d in ("ws", "runner-temp", "log"):
            (self.tmp / d).mkdir()

    def review(self, codex=None, codex_exit=0, claude=None, notes="", extra_allow=""):
        import os
        import subprocess
        env = {"PATH": f"{self.tmp / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin", "GITHUB_BASE_REF": "main",
               "GITHUB_WORKSPACE": str(self.tmp / "ws"), "RUNNER_TEMP": str(self.tmp / "runner-temp"),
               "GITHUB_OUTPUT": str(self.tmp / "output"), "FAKE_LOG": str(self.tmp / "log"),
               "FAKE_CODEX_EXIT": str(codex_exit), "HOME": str(self.tmp),
               "REPO_NOTES": notes, "EXTRA_ALLOW": extra_allow}
        if codex is not None:
            env["FAKE_CODEX_ANSWER"] = codex
        if claude is not None:
            env["FAKE_CLAUDE_ANSWER"] = claude
        (self.tmp / "output").write_text("")
        run = subprocess.run(["bash", "-c", self.script], cwd=self.tmp / "ws", env=env,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        outputs = dict(line.split("=", 1) for line in (self.tmp / "output").read_text().splitlines())
        calls_file = self.tmp / "log" / "claude-calls"
        claude_calls = len(calls_file.read_text().split()) if calls_file.exists() else 0
        display = (self.tmp / "ws" / "review_display.md").read_text()
        return outputs, claude_calls, display

    def test_a_clean_codex_answer_passes(self):
        out, calls, display = self.review(codex=self.CLEAN)
        self.assertEqual(out, {"reviewer": "codex", "verdict": "pass"})
        self.assertEqual(calls, 0)
        self.assertIn("No findings.", display)

    def test_any_codex_finding_fails(self):
        out, calls, display = self.review(codex=self.FINDING)
        self.assertEqual(out, {"reviewer": "codex", "verdict": "needs_changes"})
        self.assertIn("- **[P1]** `stats.py:3`: Skips the first element.", display)

    def test_codex_gets_the_schema(self):
        self.review(codex=self.CLEAN)
        import json
        schema = json.loads((self.tmp / "log" / "codex-schema.json").read_text())
        self.assertEqual(schema["required"], ["diff_inspected", "summary", "findings"])

    def test_prose_from_codex_is_not_a_verdict(self):
        out, calls, _ = self.review(codex="No actionable defect is evident in the changed manifest.",
                                    claude=self.CLEAN)
        self.assertEqual(out["verdict"], "pass")
        self.assertTrue(out["reviewer"].startswith("claude (Codex gave no usable review: its answer didn't match"))
        self.assertEqual(calls, 1)

    def test_an_answer_that_did_not_read_the_diff_is_unusable(self):
        unread = self.CLEAN.replace("true", "false")
        out, calls, _ = self.review(codex=unread, claude=unread)
        self.assertEqual(out["verdict"], "needs_changes")
        self.assertTrue(out["reviewer"].startswith("none"))
        self.assertEqual(calls, 3)

    def test_unexpected_or_missing_fields_are_unusable(self):
        for bad in ['{"diff_inspected": true, "summary": "x", "findings": [], "verdict": "pass"}',
                    '{"diff_inspected": true, "findings": []}',
                    '{"diff_inspected": true, "summary": "x", "findings": [{"priority": "P1"}]}',
                    '{"diff_inspected": true, "summary": "x", "findings": "none"}',
                    '{"diff_inspected": "yes", "summary": "x", "findings": []}',
                    '[]']:
            with self.subTest(bad=bad):
                out, _, _ = self.review(codex=bad, claude=self.FINDING)
                self.assertTrue(out["reviewer"].startswith("claude"), out)
                self.assertEqual(out["verdict"], "needs_changes")

    def test_a_placeholder_summary_is_unusable(self):
        # homeassistant #131: a Claude answer whose whole summary was "test".
        placeholder = '{"diff_inspected": true, "summary": "test", "findings": []}'
        out, calls, _ = self.review(codex=placeholder, claude=placeholder)
        self.assertEqual(out["verdict"], "needs_changes")
        self.assertTrue(out["reviewer"].startswith("none"))
        self.assertEqual(calls, 3)

    def test_caller_notes_and_rules_arrive_as_plain_data(self):
        notes = "Large data files include ground_truth.json. $(touch pwned) `touch pwned2`"
        rule = "Bash(python3 scripts/check.py --check*)"
        self.review(codex_exit=1, claude=self.CLEAN, notes=notes, extra_allow=rule)
        prompt = (self.tmp / "log" / "codex-prompt").read_text()
        self.assertIn("unless this prompt names it. " + notes, prompt)
        self.assertEqual((self.tmp / "log" / "claude-allow").read_text().split(",")[-1], rule)
        self.assertEqual(list((self.tmp / "ws").glob("pwned*")), [])

    def test_no_notes_or_rules_leave_no_trace(self):
        self.review(codex_exit=1, claude=self.CLEAN)
        self.assertTrue((self.tmp / "log" / "codex-prompt").read_text().count("unless this prompt names it.\n"))
        self.assertTrue((self.tmp / "log" / "claude-allow").read_text().endswith("Bash(pwd*)"))

    def test_codex_exiting_with_an_error_falls_back_to_claude(self):
        out, calls, display = self.review(codex_exit=1, claude=self.FINDING)
        self.assertEqual(out, {"reviewer": "claude (Codex gave no usable review: it exited with status 1)",
                               "verdict": "needs_changes"})
        self.assertIn("[P1]", display)
        import json
        schema = json.loads((self.tmp / "log" / "claude-schema.json").read_text())
        self.assertEqual(schema["required"], ["diff_inspected", "summary", "findings"])

    def test_claude_prose_without_structured_output_is_not_a_verdict(self):
        out, calls, display = self.review(codex_exit=1)
        self.assertEqual(out, {"reviewer": "none (neither Codex nor Claude gave a usable review)",
                               "verdict": "needs_changes"})
        self.assertEqual(calls, 3)
        self.assertIn("Neither reviewer gave a usable review: Codex it exited with status 1", display)


# gh api stand-in: GETs answer from the $FAKE_GH JSON map (keyed by the
# full path, else the path without its query string), POSTs are recorded in
# $FAKE_LOG/gh-posts.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if "--method" in args:
    body = sys.stdin.read() if "body=@-" in args else next(a[5:] for a in args if a.startswith("body="))
    with open(os.path.join(os.environ["FAKE_LOG"], "gh-posts"), "a") as fh:
        fh.write(json.dumps({"path": args[3], "body": body}) + "\\n")
    sys.exit(0)
responses = json.load(open(os.environ["FAKE_GH"]))
answer = responses.get(args[1], responses.get(args[1].split("?")[0]))
if answer is None:
    sys.exit(1)
sys.stdout.write(answer if isinstance(answer, str) else json.dumps(answer))
"""
# Records its arguments (one per line) and where it started, runs $FAKE_FIX
# there, prints $FAKE_SUMMARY (or a stock summary) and exits with
# $FAKE_CLAUDE_EXIT.
FIX_CLAUDE = """#!/bin/sh
for arg; do printf '%s\\n' "$arg"; done > "$FAKE_LOG/claude-args"
pwd > "$FAKE_LOG/claude-cwd"
sh -c "${FAKE_FIX:-true}"
echo "${FAKE_SUMMARY:-Claude changed what the log asked for.}"
exit "${FAKE_CLAUDE_EXIT:-0}"
"""


class FixDecide(unittest.TestCase):
    """The decide step of the rendered fix.yml: a fix starts only when every
    check on the PR's head has finished, one failed, the head hasn't been
    tried, and the PR has had fewer than three fix attempts."""

    REPO = "alex-mf-montiel/agentic-workflow"

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        self.script = step_script(FIX.read_text(), "decide")
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "gh").write_text(FAKE_GH)
        (self.tmp / "bin" / "gh").chmod(0o755)

    def decide(self, checks, comments=(), commit_messages=("Add stats",), prs=None, extra=None):
        import json
        import subprocess
        r = f"repos/{self.REPO}"
        if prs is None:
            prs = [{"number": 7, "draft": True, "head": {"sha": "abc123", "ref": "feature"}, "base": {"ref": "main"}}]
        (self.tmp / "gh.json").write_text(json.dumps({
            f"{r}/pulls": prs,
            f"{r}/commits/abc123/check-runs": {"total_count": len(checks), "check_runs": checks},
            **(extra or {}),
            f"{r}/issues/7/comments": [{"body": b} for b in comments],
            f"{r}/pulls/7/commits": [{"commit": {"message": m}} for m in commit_messages],
        }))
        (self.tmp / "output").write_text("")
        env = {"PATH": f"{self.tmp / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin",
               "GITHUB_REPOSITORY": self.REPO, "HEAD_BRANCH": "feature",
               "GITHUB_OUTPUT": str(self.tmp / "output"), "FAKE_GH": str(self.tmp / "gh.json"),
               "FAKE_LOG": str(self.tmp)}
        run = subprocess.run(["bash", "-c", self.script], env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        outputs = dict(line.split("=", 1) for line in (self.tmp / "output").read_text().splitlines())
        posts = (self.tmp / "gh-posts").read_text().splitlines() if (self.tmp / "gh-posts").exists() else []
        return outputs, [json.loads(p) for p in posts]

    @staticmethod
    def check(name, conclusion, status="completed", id=11):
        return {"id": id, "name": name, "status": status, "conclusion": conclusion}

    def test_a_failed_check_starts_a_fix(self):
        import json
        out, posts = self.decide([self.check("validate", "failure"), self.check("review", "success", id=12)])
        self.assertEqual(out["fix"], "true")
        self.assertEqual((out["number"], out["sha"], out["branch"], out["base"], out["attempt"]),
                         ("7", "abc123", "feature", "main", "1"))
        self.assertEqual(json.loads(out["failed"]), [{"id": 11, "name": "validate"}])
        self.assertEqual(posts, [])

    def test_waits_while_any_check_is_running(self):
        out, _ = self.decide([self.check("validate", "failure"), self.check("review", None, "in_progress")])
        self.assertEqual(out, {"fix": "false"})

    def test_passing_or_cancelled_checks_need_no_fix(self):
        for conclusion in ("success", "cancelled", "skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                out, _ = self.decide([self.check("validate", conclusion)])
                self.assertEqual(out, {"fix": "false"})

    def test_a_timeout_counts_as_a_failure(self):
        out, _ = self.decide([self.check("review", "timed_out")])
        self.assertEqual(out["fix"], "true")

    def test_a_head_is_only_tried_once(self):
        out, _ = self.decide([self.check("validate", "failure")], comments=["x\n<!-- auto-fix: abc123 -->"])
        self.assertEqual(out, {"fix": "false"})
        out, _ = self.decide([self.check("validate", "failure")], comments=["<!-- auto-fix: older -->"])
        self.assertEqual(out["fix"], "true")

    def test_counts_earlier_attempts_and_stops_after_three(self):
        fixed = "Fix failing checks: validate\n\nAuto-fix-attempt: {}"
        out, _ = self.decide([self.check("validate", "failure")], commit_messages=["Add", fixed.format(1)])
        self.assertEqual(out["attempt"], "2")
        out, posts = self.decide([self.check("validate", "failure")],
                                 commit_messages=["Add"] + [fixed.format(n) for n in (1, 2, 3)])
        self.assertEqual(out, {"fix": "false"})
        self.assertEqual(len(posts), 1)
        self.assertIn("stopped", posts[0]["body"])
        self.assertIn("<!-- auto-fix: abc123 -->", posts[0]["body"])

    def test_reads_every_page_of_checks(self):
        page = f"repos/{self.REPO}/commits/abc123/check-runs?per_page=100&page="
        first = [self.check(f"shard {n}", "success", id=n) for n in range(100)]
        for late in (self.check("shard 100", None, "in_progress", id=100), self.check("shard 100", "failure", id=100)):
            with self.subTest(late=late["status"]):
                out, _ = self.decide([], extra={
                    page + "1": {"total_count": 101, "check_runs": first},
                    page + "2": {"total_count": 101, "check_runs": [late]}})
                self.assertEqual(out["fix"], "true" if late["conclusion"] == "failure" else "false")

    def test_a_pr_marked_ready_is_left_alone(self):
        ready = [{"number": 7, "draft": False, "head": {"sha": "abc123", "ref": "feature"}, "base": {"ref": "main"}}]
        out, posts = self.decide([self.check("validate", "failure")], prs=ready)
        self.assertEqual(out, {"fix": "false"})
        self.assertEqual(posts, [])

    def test_no_open_pr_means_nothing_to_fix(self):
        out, _ = self.decide([self.check("validate", "failure")], prs=[])
        self.assertEqual(out, {"fix": "false"})


class FixCommit(unittest.TestCase):
    """The fix and push steps of the rendered fix.yml, against a real git
    repository with a local remote: only the fixer's file changes are pushed,
    and the guards hold."""

    REPO = "alex-mf-montiel/agentic-workflow"
    TOKEN = "sk-refresh-0123456789abcdefghij"

    def setUp(self):
        import json
        import subprocess
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        text = FIX.read_text()
        self.fix, self.push, self.report = (step_script(text, i) for i in ("fix", "push", "report"))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in [("gh", FAKE_GH), ("review-sandbox", FAKE_SANDBOX), ("claude", FIX_CLAUDE)]:
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        (self.tmp / "home" / ".codex").mkdir(parents=True)
        (self.tmp / "home" / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": self.TOKEN}}))
        (self.tmp / "runner-temp").mkdir()
        (self.tmp / "gh.json").write_text(json.dumps({
            f"repos/{self.REPO}/actions/jobs/11/logs": "2026-10-28T00:45:04.2355267Z error: average skips the first element\n"}))

        def git(*args, cwd):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
        self.git = git
        git("init", "-q", "--bare", "-b", "feature", str(self.tmp / "remote.git"), cwd=self.tmp)
        seed = self.tmp / "seed"
        (seed / ".github" / "workflows").mkdir(parents=True)
        (seed / "stats.py").write_text("def average(xs):\n    return sum(xs[1:]) / len(xs)\n")
        (seed / ".github" / "workflows" / "ci.yml").write_text("name: CI\n")
        git("init", "-q", "-b", "feature", cwd=seed)
        git("add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Add stats", cwd=seed)
        git("push", "-q", str(self.tmp / "remote.git"), "feature", "feature:main", cwd=seed)
        git("clone", "-q", str(self.tmp / "remote.git"), str(self.tmp / "ws"), cwd=self.tmp)

    def run_steps(self, **fake):
        import subprocess
        env = {"PATH": f"{self.tmp / 'bin'}:{Path(sys.executable).parent}:/opt/homebrew/bin:/usr/bin:/bin",
               "HOME": str(self.tmp / "home"), "GITHUB_REPOSITORY": self.REPO,
               "GITHUB_WORKSPACE": str(self.tmp / "ws"), "RUNNER_TEMP": str(self.tmp / "runner-temp"),
               "GITHUB_OUTPUT": str(self.tmp / "output"), "FAKE_GH": str(self.tmp / "gh.json"),
               "FAKE_LOG": str(self.tmp), "BASE": "main", "NAMES": "test",
               "FAILED": '[{"id": 11, "name": "test"}]', "BRANCH": "feature", "ATTEMPT": "1",
               "NUMBER": "7", "SHA": "abc123", "PAT": "pat-not-for-the-fixer", **fake}
        (self.tmp / "output").write_text("")
        for script in (self.fix, self.push):
            run = subprocess.run(["bash", "-c", script], cwd=self.tmp / "ws", env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            outputs = dict(line.split("=", 1) for line in (self.tmp / "output").read_text().splitlines())
            env.update(FIXER=outputs.get("fixer", ""))
            if not env["FIXER"]:
                break
        env.update(RESULT=outputs.get("result", ""))
        run = subprocess.run(["bash", "-c", self.report], cwd=self.tmp / "ws", env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return outputs

    def remote(self, *args):
        import subprocess
        return subprocess.run(["git", *args], cwd=self.tmp / "remote.git", check=True,
                              capture_output=True, text=True).stdout

    def comment(self):
        import json
        return json.loads((self.tmp / "gh-posts").read_text().splitlines()[-1])["body"]

    FIX = "sed -i.bak 's/xs\\[1:\\]/xs/' stats.py && rm stats.py.bak"

    def test_the_fix_is_committed_and_pushed(self):
        out = self.run_steps(FAKE_FIX=self.FIX)
        self.assertTrue(out["result"].startswith("pushed "), out)
        self.assertEqual(self.remote("show", "feature:stats.py"), "def average(xs):\n    return sum(xs) / len(xs)\n")
        message = self.remote("log", "-1", "--format=%an|%B", "feature")
        self.assertTrue(message.startswith("Claude|Fix failing checks: test\n"), message)
        self.assertIn("Auto-fix-attempt: 1", message)
        # Only the fixer's change: nothing the job wrote, no copy of .git.
        self.assertEqual(self.remote("show", "--name-only", "--format=", "feature").split(), ["stats.py"])
        args = (self.tmp / "claude-args").read_text()
        self.assertIn("error: average skips the first element", args)
        self.assertNotIn("pat-not-for-the-fixer", args)
        # It worked in the copy, not the checkout.
        self.assertEqual(Path((self.tmp / "claude-cwd").read_text().strip()).resolve(),
                         (self.tmp / "runner-temp" / "fix-out" / "work").resolve())
        self.assertIn("<!-- auto-fix: abc123 -->", self.comment())
        self.assertIn("Result: pushed", self.comment())

    def test_no_change_pushes_nothing(self):
        before = self.remote("rev-parse", "feature")
        out = self.run_steps()
        self.assertEqual(out["result"], "no change")
        self.assertEqual(self.remote("rev-parse", "feature"), before)
        self.assertIn("<!-- auto-fix: abc123 -->", self.comment())

    def test_workflow_changes_are_not_pushed(self):
        before = self.remote("rev-parse", "feature")
        out = self.run_steps(FAKE_FIX=self.FIX + " && echo '# x' >> .github/workflows/ci.yml")
        self.assertEqual(out["result"], "not pushed: the change touched .github/workflows")
        self.assertEqual(self.remote("rev-parse", "feature"), before)

    def test_a_login_token_is_never_pushed(self):
        before = self.remote("rev-parse", "feature")
        out = self.run_steps(FAKE_FIX=f"cp {self.tmp / 'home' / '.codex' / 'auth.json'} leaked.json")
        self.assertEqual(out["result"], "not pushed: the change contained a model login token")
        self.assertEqual(self.remote("rev-parse", "feature"), before)

    def test_a_login_token_in_the_summary_is_never_published(self):
        out = self.run_steps(FAKE_FIX=self.FIX, FAKE_SUMMARY=f"Fixed it. Also: {self.TOKEN}")
        self.assertTrue(out["result"].startswith("pushed "), out)
        message = self.remote("log", "-1", "--format=%B", "feature")
        self.assertNotIn(self.TOKEN, message)
        self.assertIn("summary is withheld", message)
        self.assertNotIn(self.TOKEN, self.comment())
        self.assertIn("summary is withheld", self.comment())

    def test_the_fixer_gets_file_tools_in_its_copy_and_no_commands(self):
        self.run_steps()
        args = (self.tmp / "claude-args").read_text().splitlines()
        work = self.tmp / "runner-temp" / "fix-out" / "work"
        allow = args[args.index("--allowedTools") + 1].split(",")
        self.assertEqual(allow, [f"{tool}(/{work}/**)" for tool in ("Read", "Edit", "Write", "Grep", "Glob")])
        self.assertIn("Bash", args[args.index("--disallowedTools") + 1].split(","))
        # It runs no git, so the PR's diff comes in the prompt.
        prompt = args[args.index("-p") + 1:args.index("--effort")]
        self.assertIn("## The PR's change (git diff origin/main...HEAD)", "\n".join(prompt))

    def test_a_failed_fixer_pushes_nothing_but_reports(self):
        before = self.remote("rev-parse", "feature")
        self.run_steps(FAKE_FIX=self.FIX, FAKE_CLAUDE_EXIT="1")
        self.assertEqual(self.remote("rev-parse", "feature"), before)
        self.assertIn("no fixer ran", self.comment())
        self.assertIn("<!-- auto-fix: abc123 -->", self.comment())



if __name__ == "__main__":
    unittest.main()
