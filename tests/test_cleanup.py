"""Tests for the reusable cleanup workflow (cleanup.yml) and the fixer's rule
that reverts a cleanup commit which broke a check. The step scripts run
against a stand-in gh, a stand-in sandbox and Claude, a real git remote and
the real, pinned lizard, which the install step fetches once (the tests that
need it are skipped when it can't be installed).
Run: python3 -m unittest discover -s tests"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_workflows import FAKE_GH, FAKE_SANDBOX, FIX, WORKFLOWS, step_script

CLEANUP = WORKFLOWS / "cleanup.yml"
REPO = "alex-mf-montiel/agentic-workflow"
BASE_PATH = f"{Path(sys.executable).parent}:/opt/homebrew/bin:/usr/bin:/bin"

# Records its arguments (one per line) and where it started, runs $FAKE_EDIT
# there, prints $FAKE_SUMMARY (or a stock summary) and exits with
# $FAKE_CLAUDE_EXIT.
CLEANUP_CLAUDE = """#!/bin/sh
for arg; do printf '%s\\n' "$arg"; done > "$FAKE_LOG/claude-args"
pwd > "$FAKE_LOG/claude-cwd"
sh -c "${FAKE_EDIT:-true}"
echo "${FAKE_SUMMARY:-- Flattened classify with early returns.}"
exit "${FAKE_CLAUDE_EXIT:-0}"
"""

# A PR's version of stats.py: nested conditionals and a duplicated function,
# well over the 40 changed lines the cleanup needs.
TANGLED = '''def classify(values):
    result = []
    for value in values:
        if value is not None:
            if value > 0:
                if value > 100:
                    result.append("big")
                else:
                    result.append("small")
            else:
                result.append("non-positive")
        else:
            result.append("missing")
    return result


def classify_again(values):
    result = []
    for value in values:
        if value is not None:
            if value > 0:
                if value > 100:
                    result.append("big")
                else:
                    result.append("small")
            else:
                result.append("non-positive")
        else:
            result.append("missing")
    return result


def total(values):
    count = 0
    for value in values:
        if value is not None:
            count = count + value
    return count


def mean(values):
    present = [value for value in values if value is not None]
    if len(present) == 0:
        return 0
    return total(present) / len(present)
'''

SIMPLE = '''def label(value):
    if value is None:
        return "missing"
    if value <= 0:
        return "non-positive"
    return "big" if value > 100 else "small"


def classify(values):
    return [label(value) for value in values]


def classify_again(values):
    return classify(values)


def total(values):
    return sum(value for value in values if value is not None)


def mean(values):
    present = [value for value in values if value is not None]
    if not present:
        return 0
    return total(present) / len(present)
'''

_LIZARD = {}


def lizard_venv():
    """Runs cleanup.yml's own install step once, so the pinned URLs and
    hashes are the ones tested. None when it can't (no network, say)."""
    if "venv" not in _LIZARD:
        temp = Path(tempfile.mkdtemp())
        env = {"PATH": BASE_PATH, "RUNNER_TEMP": str(temp), "HOME": str(temp)}
        run = subprocess.run(["bash", "-c", step_script(CLEANUP.read_text(), "install")],
                             env=env, capture_output=True, text=True)
        _LIZARD["venv"] = temp / "cleanup-lizard" if run.returncode == 0 else None
        _LIZARD["error"] = run.stderr[-2000:]
    return _LIZARD["venv"]


def outputs_of(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


class CleanupGuards(unittest.TestCase):
    def test_claude_only_runs_inside_the_sandbox_with_file_tools(self):
        text = CLEANUP.read_text()
        code = [line.strip() for line in text.splitlines() if not line.strip().startswith("#")]
        launch = next(i for i, line in enumerate(code) if line.startswith("claude -p"))
        self.assertIn("review-sandbox claude", " ".join(code[launch - 2:launch]))
        self.assertIn('--disallowedTools "Bash,Write,NotebookEdit,WebFetch,WebSearch"', text)
        self.assertIn("--model opus", text)

    def test_the_push_token_only_reaches_the_push(self):
        text = CLEANUP.read_text()
        before_push = text.split("id: push\n", 1)[0]
        self.assertNotIn("READY_FOR_REVIEW_PAT }}", before_push)
        self.assertIn('GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth"', text)

    def test_no_step_can_fail_the_check(self):
        # A failed cleanup check would block the PR and start the fixer.
        text = CLEANUP.read_text()
        steps = text.split("    steps:\n", 1)[1].split("\n      - ")
        self.assertEqual(len(steps), 8)
        for step in steps:
            self.assertIn("continue-on-error: true", step, step.splitlines()[0])
        self.assertIn("timeout-minutes: 15", text.split("id: cleanup\n", 1)[1].split("run: |", 1)[0])

    def test_code_that_reads_the_pr_runs_isolated(self):
        text = CLEANUP.read_text()
        # Every python3, including ones that only read this job's own files:
        # a step's working directory is the PR's checkout, and `python3 -`
        # imports from there first (a planted json.py, say).
        calls = re.findall(r"python3 (?!-m venv)\S*", "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")))
        self.assertGreaterEqual(len(calls), 8)
        for call in calls:
            self.assertEqual(call, "python3 -I", calls)
        self.assertNotIn("pip install", text)

    def test_caller_inputs_reach_scripts_only_through_the_environment(self):
        for line in CLEANUP.read_text().splitlines():
            if "${{ inputs." in line:
                self.assertRegex(line.strip(), r"^runs-on: ", line.strip())


class CleanupDecide(unittest.TestCase):
    """The decide step: the cleanup runs once per PR, on a draft from this
    repository, for its current head, before any review."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "gh").write_text(FAKE_GH)
        (self.tmp / "bin" / "gh").chmod(0o755)

    def decide(self, draft=True, head_repo=REPO, sha="abc123", comments=(), commits=("Add stats",)):
        r = f"repos/{REPO}"
        (self.tmp / "gh.json").write_text(json.dumps({
            f"{r}/pulls/7": {"state": "open", "draft": draft, "base": {"ref": "main"},
                             "head": {"sha": "abc123", "ref": "feature", "repo": {"full_name": head_repo}}},
            f"{r}/issues/7/comments": [{"body": body, "user": {"login": login}} for login, body in comments],
            f"{r}/pulls/7/commits": [{"commit": {"message": m}} for m in commits],
        }))
        (self.tmp / "output").write_text("")
        env = {"PATH": f"{self.tmp / 'bin'}:{BASE_PATH}", "GITHUB_REPOSITORY": REPO, "NUMBER": "7",
               "EVENT_SHA": sha, "GITHUB_OUTPUT": str(self.tmp / "output"),
               "FAKE_GH": str(self.tmp / "gh.json"), "FAKE_LOG": str(self.tmp)}
        run = subprocess.run(["bash", "-c", step_script(CLEANUP.read_text(), "decide")],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return outputs_of(self.tmp / "output"), run.stdout

    def test_a_fresh_draft_is_cleaned_up(self):
        out, _ = self.decide()
        self.assertEqual(out, {"run": "true", "sha": "abc123", "branch": "feature", "base": "main"})

    def test_a_pr_marked_ready_is_left_alone(self):
        self.assertEqual(self.decide(draft=False)[0], {"run": "false"})

    def test_a_pr_from_another_repository_is_left_alone(self):
        self.assertEqual(self.decide(head_repo="someone/fork")[0], {"run": "false"})

    def test_a_run_for_an_older_commit_leaves_it_to_the_newer_one(self):
        self.assertEqual(self.decide(sha="older")[0], {"run": "false"})

    def test_it_never_runs_after_the_review_has_started(self):
        review = ("github-actions[bot]", "**Independent review** (codex) - verdict: needs_changes")
        out, said = self.decide(comments=[review])
        self.assertEqual(out, {"run": "false"})
        self.assertIn("only runs before it", said)
        # Someone else quoting a review isn't a review.
        self.assertEqual(self.decide(comments=[("someone", review[1])])[0]["run"], "true")

    def test_it_runs_once_per_pr(self):
        done = ("github-actions[bot]", "**Cleanup pass** (Claude) - ...\n\n<!-- cleanup-pass -->")
        self.assertEqual(self.decide(comments=[done])[0], {"run": "false"})
        for trailer in ("applied", "reverted"):
            with self.subTest(trailer=trailer):
                out, _ = self.decide(commits=["Add stats", f"Clean up\n\nCleanup-pass: {trailer}"])
                self.assertEqual(out, {"run": "false"})


class CleanupSteps(unittest.TestCase):
    """The select, cleanup, keep, push and report steps against a real git
    remote and the real lizard."""

    def setUp(self):
        self.venv = lizard_venv()
        if self.venv is None:
            self.skipTest("lizard couldn't be installed: " + _LIZARD["error"])
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.text = CLEANUP.read_text()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in [("gh", FAKE_GH), ("review-sandbox", FAKE_SANDBOX), ("claude", CLEANUP_CLAUDE)]:
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        (self.tmp / "home").mkdir()
        (self.tmp / "runner-temp").mkdir()
        (self.tmp / "runner-temp" / "cleanup-lizard").symlink_to(self.venv)
        (self.tmp / "gh.json").write_text("{}")
        self.git("init", "-q", "--bare", "-b", "main", str(self.tmp / "remote.git"), cwd=self.tmp)
        seed = self.tmp / "seed"
        (seed / "www").mkdir(parents=True)
        (seed / ".github" / "workflows").mkdir(parents=True)
        (seed / "stats.py").write_text("def mean(values):\n    return sum(values) / len(values)\n")
        (seed / "README.md").write_text("# Stats\n")
        (seed / ".gitattributes").write_text("www/** linguist-vendored\n")
        (seed / "www" / "bundle.js").write_text("function a() { return 1; }\n")
        self.git("init", "-q", "-b", "main", cwd=seed)
        self.commit(seed, "Start")
        self.git("checkout", "-q", "-b", "feature", cwd=seed)
        (seed / "stats.py").write_text(TANGLED)
        (seed / "README.md").write_text("# Stats\n\n" + "More words.\n" * 60)
        (seed / "www" / "bundle.js").write_text("".join(f"function f{i}(x) {{ if (x) {{ return {i}; }} return 0; }}\n"
                                                        for i in range(60)))
        (seed / "min.js").write_text("var a=1;" * 200 + "\n" + "function g(x){return x}\n" * 50)
        (seed / ".github" / "check.py").write_text("print('hi')\n" * 50)
        self.commit(seed, "Add stats")
        self.git("push", "-q", str(self.tmp / "remote.git"), "main", "feature", cwd=seed)
        self.git("clone", "-q", "-b", "feature", str(self.tmp / "remote.git"), str(self.tmp / "ws"), cwd=self.tmp)

    @staticmethod
    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    def commit(self, where, message):
        self.git("add", "-A", cwd=where)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message, cwd=where)

    def run_steps(self, **fake):
        """Runs the steps in order, the way GitHub would: a step whose if is
        false is skipped, and a failing step doesn't stop the job."""
        env = {"PATH": f"{self.tmp / 'bin'}:{BASE_PATH}", "HOME": str(self.tmp / "home"),
               "GITHUB_REPOSITORY": REPO, "GITHUB_WORKSPACE": str(self.tmp / "ws"),
               "RUNNER_TEMP": str(self.tmp / "runner-temp"), "GITHUB_OUTPUT": str(self.tmp / "output"),
               "FAKE_GH": str(self.tmp / "gh.json"), "FAKE_LOG": str(self.tmp), "BASE": "main",
               "BRANCH": "feature", "NUMBER": "7", "PAT": "pat-not-for-claude", **fake}
        out, outcome = {}, {}

        def step(step_id, extra=None):
            (self.tmp / "output").write_text("")
            run = subprocess.run(["bash", "-c", step_script(self.text, step_id)], cwd=self.tmp / "ws",
                                 env={**env, **(extra or {})}, capture_output=True, text=True)
            outcome[step_id] = "success" if run.returncode == 0 else "failure"
            out[step_id] = outputs_of(self.tmp / "output")
            return out[step_id]

        if step("select").get("go") == "true":
            step("cleanup")
        if out.get("cleanup", {}).get("ran") == "true":
            step("keep")
        if out.get("keep", {}).get("commit") == "true":
            step("push")
        if "cleanup" in outcome:
            step("report", {"RAN": out["cleanup"].get("ran", ""), "RESULT": out.get("keep", {}).get("result", ""),
                            "COMMITTED": out.get("keep", {}).get("commit", ""),
                            "PUSHED": out.get("push", {}).get("pushed", ""),
                            "COMMIT": out.get("push", {}).get("commit", "")})
        return out

    def remote(self, *args):
        return self.git(*args, cwd=self.tmp / "remote.git")

    def comment(self):
        posts = self.tmp / "gh-posts"
        return json.loads(posts.read_text().splitlines()[-1])["body"] if posts.exists() else None

    def write(self, path, text):
        """A FAKE_EDIT that writes text to path in Claude's working copy."""
        source = self.tmp / f"edit-{path.replace('/', '-')}"
        source.write_text(text)
        return f"cp {source} {path}"

    def test_only_the_prs_own_code_files_are_picked(self):
        out = self.run_steps()
        self.assertEqual(out["select"]["go"], "true")
        files = json.loads((self.tmp / "runner-temp" / "cleanup-out" / "files.json").read_text())
        # Not the docs, the vendored bundle, the minified file or .github/.
        self.assertEqual(files, ["stats.py"])
        numstat = self.git("diff", "--numstat", "origin/main...HEAD", "--", "stats.py", cwd=self.tmp / "ws").split()
        self.assertEqual(out["select"]["lines"], str(int(numstat[0]) + int(numstat[1])))

    def test_a_small_pr_is_skipped_without_a_comment(self):
        ws = self.tmp / "ws"
        self.git("reset", "-q", "--hard", "origin/main", cwd=ws)
        (ws / "stats.py").write_text("def mean(values):\n    if not values:\n        return 0\n"
                                     "    return sum(values) / len(values)\n")
        self.commit(ws, "Guard empty")
        out = self.run_steps()
        self.assertEqual(out["select"], {"go": "false"})
        self.assertNotIn("cleanup", out)
        self.assertIsNone(self.comment())

    def test_a_simpler_version_is_committed_pushed_and_reported(self):
        before = self.remote("rev-parse", "feature")
        out = self.run_steps(FAKE_EDIT=self.write("stats.py", SIMPLE))
        self.assertEqual(out["keep"]["result"], "kept")
        self.assertEqual(out["push"]["pushed"], "true")
        self.assertEqual(self.remote("rev-parse", "feature^"), before)
        self.assertEqual(self.remote("show", "feature:stats.py"), SIMPLE)
        message = self.remote("log", "-1", "--format=%an|%B", "feature")
        self.assertTrue(message.startswith("Claude|Clean up the PR's code before review\n"), message)
        self.assertIn("Cleanup-pass: applied", message)
        self.assertIn("| Lines of code |", message)
        comment = self.comment()
        self.assertIn("**Cleanup pass** (Claude) - Pushed", comment)
        self.assertIn("| Most complex function |", comment)
        self.assertIn("Flattened classify", comment)
        self.assertIn("<!-- cleanup-pass -->", comment)

    def test_only_the_prs_code_files_reach_the_commit(self):
        edit = self.write("stats.py", SIMPLE) + " && echo extra >> README.md && echo x > new.py"
        out = self.run_steps(FAKE_EDIT=edit)
        self.assertEqual(out["push"]["pushed"], "true")
        self.assertEqual(self.remote("show", "--name-only", "--format=", "feature").split(), ["stats.py"])

    def test_claude_gets_edit_for_the_prs_files_only_and_works_in_a_copy(self):
        self.run_steps()
        args = (self.tmp / "claude-args").read_text().splitlines()
        work = self.tmp / "runner-temp" / "cleanup-out" / "work"
        allow = args[args.index("--allowedTools") + 1].split(",")
        self.assertEqual(allow, [f"Read(/{work}/**)", f"Grep(/{work}/**)", f"Glob(/{work}/**)",
                                 f"Edit(/{work}/stats.py)"])
        self.assertEqual(args[args.index("--model") + 1], "opus")
        prompt = "\n".join(args[args.index("-p") + 1:args.index("--model")])
        self.assertIn("- stats.py", prompt)
        self.assertIn("## The PR's change (git diff origin/main...HEAD)", prompt)
        self.assertIn('result.append("missing")', prompt)
        self.assertNotIn("More words.", prompt)
        self.assertNotIn("pat-not-for-claude", "\n".join(args))
        self.assertEqual(Path((self.tmp / "claude-cwd").read_text().strip()).resolve(), work.resolve())

    def test_a_change_that_makes_the_numbers_worse_is_not_kept(self):
        worse = TANGLED.replace('    return result\n',
                                '    if result:\n        if len(result) > 1:\n            return result\n'
                                '    return result\n', 1)
        before = self.remote("rev-parse", "feature")
        out = self.run_steps(FAKE_EDIT=self.write("stats.py", worse))
        self.assertTrue(out["keep"]["result"].startswith("not kept: it made these worse: "), out["keep"])
        self.assertEqual(self.remote("rev-parse", "feature"), before)
        self.assertIn("Nothing changed: not kept", self.comment())
        self.assertIn("<!-- cleanup-pass -->", self.comment())

    def test_nothing_worth_changing_pushes_nothing(self):
        before = self.remote("rev-parse", "feature")
        out = self.run_steps(FAKE_SUMMARY="Nothing here is clearly worth changing.")
        self.assertEqual(out["keep"]["result"], "no change")
        self.assertEqual(self.remote("rev-parse", "feature"), before)
        self.assertIn("nothing worth simplifying", self.comment())
        self.assertIn("<!-- cleanup-pass -->", self.comment())

    def test_a_login_token_is_never_pushed_or_published(self):
        token = "sk-ant-oat01-0123456789abcdefghij"
        settings = self.tmp / "home" / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": token}}))
        before = self.remote("rev-parse", "feature")
        out = self.run_steps(FAKE_EDIT=self.write("stats.py", SIMPLE + f"\nTOKEN = '{token}'\n"),
                             FAKE_SUMMARY=f"Also {token}")
        self.assertEqual(out["keep"]["result"], "not kept: the change contained a model login token")
        self.assertEqual(self.remote("rev-parse", "feature"), before)
        self.assertNotIn(token, self.comment())

    def test_claude_failing_pushes_nothing_but_reports(self):
        before = self.remote("rev-parse", "feature")
        out = self.run_steps(FAKE_EDIT=self.write("stats.py", SIMPLE), FAKE_CLAUDE_EXIT="1")
        self.assertEqual(out["cleanup"]["ran"], "false")
        self.assertNotIn("keep", out)
        self.assertEqual(self.remote("rev-parse", "feature"), before)
        self.assertIn("Claude didn't finish", self.comment())
        self.assertIn("<!-- cleanup-pass -->", self.comment())

    def test_a_refused_push_leaves_no_marker_so_the_newer_run_retries(self):
        other = self.tmp / "other"
        self.git("clone", "-q", "-b", "feature", str(self.tmp / "remote.git"), str(other), cwd=self.tmp)
        (other / "notes.txt").write_text("moved\n")
        self.commit(other, "Move the branch")
        self.git("push", "-q", "origin", "feature", cwd=other)
        out = self.run_steps(FAKE_EDIT=self.write("stats.py", SIMPLE))
        self.assertEqual(out["keep"]["commit"], "true")
        self.assertNotIn("pushed", out["push"])
        self.assertIn("Not pushed", self.comment())
        self.assertNotIn("<!-- cleanup-pass -->", self.comment())


class FixRevertsABrokenCleanup(unittest.TestCase):
    """fix.yml's decide and revert steps for a cleanup commit that broke one
    of the repository's own checks."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "gh").write_text(FAKE_GH)
        (self.tmp / "bin" / "gh").chmod(0o755)

    @staticmethod
    def check(name, conclusion, id=11):
        return {"id": id, "name": name, "status": "completed", "conclusion": conclusion}

    def decide(self, head_checks, parent_checks, message="Clean up\n\nCleanup-pass: applied"):
        r = f"repos/{REPO}"
        (self.tmp / "gh.json").write_text(json.dumps({
            f"{r}/pulls": [{"number": 7, "draft": True, "head": {"sha": "abc123", "ref": "feature"},
                            "base": {"ref": "main"}}],
            f"{r}/commits/abc123/check-runs": {"total_count": len(head_checks), "check_runs": head_checks},
            f"{r}/commits/abc123": {"commit": {"message": message}, "parents": [{"sha": "def456"}]},
            f"{r}/commits/def456/check-runs": {"total_count": len(parent_checks), "check_runs": parent_checks},
            f"{r}/issues/7/comments": [],
            f"{r}/pulls/7/commits": [{"commit": {"message": "Add stats"}}, {"commit": {"message": message}}],
        }))
        (self.tmp / "output").write_text("")
        env = {"PATH": f"{self.tmp / 'bin'}:{BASE_PATH}", "GITHUB_REPOSITORY": REPO, "HEAD_BRANCH": "feature",
               "GITHUB_OUTPUT": str(self.tmp / "output"), "FAKE_GH": str(self.tmp / "gh.json"),
               "FAKE_LOG": str(self.tmp)}
        run = subprocess.run(["bash", "-c", step_script(FIX.read_text(), "decide")],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return outputs_of(self.tmp / "output")

    def test_a_check_the_cleanup_broke_reverts_it(self):
        out = self.decide([self.check("test", "failure"), self.check("review / review", "failure", 12)],
                          [self.check("test", "success")])
        self.assertEqual(out["fix"], "revert")
        self.assertEqual((out["number"], out["sha"], out["branch"], out["names"]), ("7", "abc123", "feature", "test"))

    def test_a_check_that_already_failed_goes_to_the_fixer(self):
        out = self.decide([self.check("test", "failure")], [self.check("test", "failure")])
        self.assertEqual(out["fix"], "true")

    def test_a_failed_review_on_the_cleanup_goes_to_the_fixer(self):
        out = self.decide([self.check("review / review", "failure")], [self.check("review / review", "skipped")])
        self.assertEqual(out["fix"], "true")

    def test_any_other_commit_goes_to_the_fixer(self):
        out = self.decide([self.check("test", "failure")], [self.check("test", "success")], message="Add stats")
        self.assertEqual(out["fix"], "true")

    def test_the_revert_undoes_the_cleanup_commit_and_pushes(self):
        git = CleanupSteps.git
        git("init", "-q", "--bare", "-b", "feature", str(self.tmp / "remote.git"), cwd=self.tmp)
        ws = self.tmp / "ws"
        ws.mkdir()
        git("init", "-q", "-b", "feature", cwd=ws)
        (ws / "stats.py").write_text(TANGLED)
        git("add", "-A", cwd=ws)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Add stats", cwd=ws)
        (ws / "stats.py").write_text(SIMPLE)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "Clean up\n\nCleanup-pass: applied", cwd=ws)
        git("remote", "add", "origin", str(self.tmp / "remote.git"), cwd=ws)
        git("push", "-q", "origin", "feature", cwd=ws)
        (self.tmp / "output").write_text("")
        env = {"PATH": BASE_PATH, "HOME": str(self.tmp), "RUNNER_TEMP": str(self.tmp),
               "GITHUB_OUTPUT": str(self.tmp / "output"), "PAT": "pat", "BRANCH": "feature", "NAMES": "test"}
        run = subprocess.run(["bash", "-c", step_script(FIX.read_text(), "revert")], cwd=ws, env=env,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(outputs_of(self.tmp / "output")["result"].startswith("reverted in "))
        remote = self.tmp / "remote.git"
        self.assertEqual(git("show", "feature:stats.py", cwd=remote), TANGLED)
        message = git("log", "-1", "--format=%an|%B", "feature", cwd=remote)
        self.assertTrue(message.startswith("Claude|Revert the cleanup pass: it broke test\n"), message)
        self.assertIn("Cleanup-pass: reverted", message)


if __name__ == "__main__":
    unittest.main()
