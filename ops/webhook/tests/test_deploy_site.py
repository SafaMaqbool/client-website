#!/usr/bin/env python3
"""Hermetic tests for ops/webhook/bin/deploy-site.

Everything runs with temporary roots and command stubs on PATH -- git,
runuser, npm, nginx and curl are simulated scripts; no production path,
no network, no repository access.

These tests must run as root (the pipeline chowns temp workspaces and
flock files); they refuse nothing else -- every path is under a fresh
tempfile.mkdtemp directory that is deleted in tearDown.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import pwd
import re
import shutil
import sqlite3  # noqa: F401  (kept for parity with sibling suite)
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BIN = TESTS_DIR.parent / "bin"
DEPLOY = BIN / "deploy-site"
TS_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def load_installer():
    from importlib.machinery import SourceFileLoader

    path = BIN / "install"
    spec = importlib.util.spec_from_loader(
        "yrds_install", SourceFileLoader("yrds_install", str(path))
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


installer = load_installer()

GIT_STUB = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
log = os.environ.get("YRDS_TEST_GIT_LOG")
def note(text):
    if log:
        with open(log, "a") as fh:
            fh.write(text + "\\n")
note("argv " + " ".join(args))
if args and args[0] == "init":
    target = args[-1]
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\\n")
    sys.exit(0)
if "fetch" in args:
    note("GIT_SSH_COMMAND=" + os.environ.get("GIT_SSH_COMMAND", ""))
    note("GIT_TERMINAL_PROMPT=" + os.environ.get("GIT_TERMINAL_PROMPT", ""))
    sys.exit(0)
if "cat-file" in args:
    sha = args[args.index("cat-file") + 2].split("^")[0]
    sys.exit(0 if sha == os.environ["YRDS_TEST_COMMIT"] else 1)
if "merge-base" in args:
    sys.exit(0 if os.environ.get("YRDS_TEST_ANCESTOR", "yes") == "yes" else 1)
if "rev-parse" in args:
    print(os.environ["YRDS_TEST_COMMIT"])
    sys.exit(0)
if "archive" in args:
    with open(os.environ["YRDS_TEST_TAR"], "rb") as fh:
        sys.stdout.buffer.write(fh.read())
    sys.exit(0)
sys.exit(0)
"""

RUNUSER_STUB = """#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
log = os.environ.get("YRDS_TEST_RUNUSER_LOG")
if log:
    with open(log, "a") as fh:
        fh.write("runuser " + " ".join(args) + "\\n")
assert args[0] == "-u", args
rest = args[args.index("--") + 1:]
sys.exit(subprocess.call(rest))
"""

NPM_STUB = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("YRDS_TEST_NPM_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({
            "args": args,
            "cwd": os.getcwd(),
            "CI": os.environ.get("CI"),
            "NODE_OPTIONS": os.environ.get("NODE_OPTIONS"),
            "HOME": os.environ.get("HOME"),
            "npm_config_cache": os.environ.get("npm_config_cache"),
        }) + "\\n")
if args and args[0] == "ci":
    sys.exit(0)
if args[:2] != ["run", "build"]:
    sys.exit(1)
if os.environ.get("YRDS_TEST_BUILD_FAIL") == "1":
    sys.stderr.write("stub build failure\\n")
    sys.exit(1)
dist = os.path.join(os.getcwd(), "dist")
os.makedirs(dist, exist_ok=True)
body = os.environ.get("YRDS_TEST_DIST_BODY", "<html>built</html>").encode()
with open(os.path.join(dist, "index.html"), "wb") as fh:
    fh.write(body)
sys.exit(0)
"""

NGINX_STUB = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
log = os.environ.get("YRDS_TEST_NGINX_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(args) + "\\n")
if args and args[0] == "-t":
    counter = os.environ.get("YRDS_TEST_NGINX_COUNTER")
    count = 0
    if counter and os.path.exists(counter):
        count = int(open(counter).read().strip() or "0")
    count += 1
    if counter:
        with open(counter, "w") as fh:
            fh.write(str(count))
    if os.environ.get("YRDS_TEST_NGINX_T_FAIL_AT") == str(count):
        sys.stderr.write("nginx: configuration file test failed (stub)\\n")
        sys.exit(1)
    sys.exit(0)
if args and args[0] == "-s":
    sys.exit(1 if os.environ.get("YRDS_TEST_NGINX_RELOAD_FAIL") == "1" else 0)
sys.exit(0)
"""

CURL_STUB = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
log = os.environ.get("YRDS_TEST_CURL_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(args) + "\\n")
if "-o" in args:
    out = args[args.index("-o") + 1]
    body_file = os.environ.get("YRDS_TEST_SMOKE_BODY_FILE")
    if body_file and os.path.exists(body_file):
        with open(body_file, "rb") as fh:
            data = fh.read()
    else:
        # default: whatever npm run build produced (smoke must match release)
        data = os.environ.get("YRDS_TEST_DIST_BODY", "<html>built</html>").encode()
    with open(out, "wb") as fh:
        fh.write(data)
sys.stdout.write(os.environ.get("YRDS_TEST_SMOKE_STATUS", "200"))
sys.exit(0)
"""

STUBS = {
    "git": GIT_STUB,
    "runuser": RUNUSER_STUB,
    "npm": NPM_STUB,
    "nginx": NGINX_STUB,
    "curl": CURL_STUB,
}


def pick_build_user():
    for candidate in ("nobody", "daemon", "games"):
        try:
            pwd.getpwnam(candidate)
            return candidate
        except KeyError:
            continue
    return None


class DeploySiteTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        if os.geteuid() != 0:
            raise unittest.SkipTest("deploy-site tests need root for hermetic chown/flock")
        self.build_user = pick_build_user()
        if self.build_user is None:
            raise unittest.SkipTest("no unprivileged system user available")
        self.root = Path(tempfile.mkdtemp(prefix="yrds-deploy-test-"))
        self.stubbin = self.root / "stubbin"
        self.stubbin.mkdir()
        for name, body in STUBS.items():
            path = self.stubbin / name
            path.write_text(body)
            path.chmod(0o755)
        self.state = self.root / "state"
        (self.state / "repos").mkdir(parents=True)
        (self.state / "work").mkdir()
        self.www = self.root / "www" / "bau"
        self.releases = self.www / "releases"
        self.releases.mkdir(parents=True)
        self.current = self.www / "current"
        self.logs = self.root / "logs"
        self.config_dir = self.root / "sites.d"
        self.config_dir.mkdir()
        ssh_dir = self.root / "ssh"
        ssh_dir.mkdir()
        self.deploy_key = ssh_dir / "bau_ed25519"
        self.deploy_key.write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nSENTINEL-KEY-MATERIAL\n"
        )
        self.known_hosts = ssh_dir / "known_hosts"
        self.known_hosts.write_text("bitbucket.org ssh-ed25519 AAAAstub\n")
        self.commit = "b" * 40
        self.tar_path = self.root / "src.tar"
        with tarfile.open(self.tar_path, "w") as tar:
            data = b'{"name":"bau","private":true}'
            info = tarfile.TarInfo("package.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        site = {
            "site": "bau",
            "repo_full_name": "uhpsoftware/7yrds-bau",
            "repo_url": "git@bitbucket.org:uhpsoftware/7yrds-bau.git",
            "branch": "main",
            "deploy_key": str(self.deploy_key),
            "known_hosts": str(self.known_hosts),
            "state_dir": str(self.state),
            "releases_dir": str(self.releases),
            "current_link": str(self.current),
            "compat_metadata": str(self.www / "RELEASE-METADATA.txt"),
            "log_dir": str(self.logs),
            "lock_file": str(self.root / "run" / "bau.lock"),
            "deploy_command": str(DEPLOY),
            "build_user": self.build_user,
            "web_url": "https://bau.example.test/",
            "kill_switch": str(self.root / "DISABLED"),
        }
        (self.config_dir / "bau.json").write_text(json.dumps(site))
        self.secret_sentinel = "SUPERSECRET-SENTINEL"
        (self.state / "sentinel.secret").write_text(self.secret_sentinel + "\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------
    def env(self, **extra):
        env = dict(os.environ)
        env.update(
            {
                "PATH": str(self.stubbin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                "YRDS_TEST_COMMIT": self.commit,
                "YRDS_TEST_TAR": str(self.tar_path),
                "YRDS_TEST_DIST_BODY": "<html>built</html>",
                "YRDS_TEST_GIT_LOG": str(self.root / "git.log"),
                "YRDS_TEST_RUNUSER_LOG": str(self.root / "runuser.log"),
                "YRDS_TEST_NPM_LOG": str(self.root / "npm.log"),
                "YRDS_TEST_NGINX_LOG": str(self.root / "nginx.log"),
                "YRDS_TEST_NGINX_COUNTER": str(self.root / "nginx.count"),
                "YRDS_TEST_CURL_LOG": str(self.root / "curl.log"),
            }
        )
        env.update(extra)
        return env

    def run_deploy(self, *extra, commit=None, source="manual", env_extra=None):
        command = [
            sys.executable,
            str(DEPLOY),
            "--config-dir",
            str(self.config_dir),
            "--site",
            "bau",
            "--commit",
            commit if commit is not None else self.commit,
            "--source",
            source,
            *extra,
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=self.env(**(env_extra or {})),
            timeout=180,
        )

    def seed_release(self, name, commit=None, activate=True, index="<html>old</html>"):
        release = self.releases / name
        release.mkdir(parents=True, exist_ok=True)
        (release / "index.html").write_text(index)
        (release / "RELEASE-METADATA.txt").write_text(
            "site: bau\ncommit: %s\nbranch: main\nsource: manual\n"
            "request-uuid: seed\nrun-id: seed\nbuild-utc: t\nactivation-utc: t\n"
            % (commit or "c" * 40)
        )
        if activate:
            tmp = Path(str(self.current) + ".tmp")
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink("releases/%s" % name, tmp)
            os.rename(tmp, self.current)
        return release

    def release_names(self):
        return sorted(
            entry.name
            for entry in self.releases.iterdir()
            if entry.is_dir() and TS_RE.match(entry.name)
        )

    def current_target(self):
        return os.readlink(self.current) if self.current.is_symlink() else None

    def npm_calls(self):
        path = self.root / "npm.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # ---- tests -----------------------------------------------------------
    def test_rejects_malformed_commit_before_anything(self):
        result = self.run_deploy(commit="not-a-sha")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), [])
        self.assertFalse((self.root / "git.log").exists())

    def test_refuses_test_modes_for_webhook_source(self):
        result = self.run_deploy("--build-only", source="webhook")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        result = self.run_deploy("--simulate-build-failure", source="webhook")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_test_modes_are_manual_test_only(self):
        for extra in (["--build-only"], ["--simulate-build-failure"]):
            for source in ("webhook", "manual"):
                result = self.run_deploy(*extra, source=source)
                self.assertEqual(
                    result.returncode, 2, (source, extra, result.stderr)
                )
        result = self.run_deploy(source="manual-test")  # requires a test mode
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), [])

    def test_run_id_dot_components_and_malformed_are_refused(self):
        for bad in (".", "..", "a/../b", "has space", "x" * 65):
            result = self.run_deploy("--run-id", bad, source="manual")
            self.assertEqual(result.returncode, 2, (bad, result.stderr))
        self.assertEqual(sorted(p.name for p in (self.state / "work").iterdir()), [])
        self.assertEqual(self.release_names(), [])

    def test_valid_dotted_run_id_still_works(self):
        result = self.run_deploy(
            "--run-id", "test.ok-1", "--build-only", source="manual-test"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.npm_calls()
        self.assertTrue(calls)
        self.assertTrue(
            calls[0]["cwd"].startswith(str(self.state / "work" / "bau" / "test.ok-1"))
        )

    def test_work_chain_is_traversable_by_but_not_writable_for_build_user(self):
        """Traversal regression (2026-09-29): receiver start left the state dir
        0700 and UMask=0077 left work/<site> 0700, so the unprivileged build
        user could not reach its workspace (npm ci -> EACCES, misreported as
        exit 4).  The chain must be traverse-only 0711 and stay usable."""
        self.seed_release("20260101T000001Z")
        os.chmod(self.state, 0o700)
        os.chmod(self.state / "work", 0o700)
        work_site = self.state / "work" / "bau"
        work_site.mkdir(exist_ok=True)
        os.chmod(work_site, 0o700)
        # the real /var/lib prefix is world-traversable; make the hermetic
        # root crossable too, so the check fails only on the chain itself
        os.chmod(self.root, 0o711)
        smoke_body = self.root / "smoke-body"
        smoke_body.write_text("<html>built</html>")
        result = self.run_deploy(
            "--run-id",
            "test-traversal-1",
            env_extra={"YRDS_TEST_SMOKE_BODY_FILE": str(smoke_body)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run_dir = work_site / "test-traversal-1"
        for path in (self.state, self.state / "work", work_site):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o711, (str(path), oct(mode)))
            self.assertEqual(path.stat().st_uid, 0)
        # the disposable workspace survives a full deploy and the build user
        # must be able to reach it through the traverse-only chain
        self.assertTrue(run_dir.is_dir(), run_dir)
        # kernel-enforced check as the unprivileged user: cross the chain,
        # write nowhere in it, and still build inside the chowned run dir
        probe = (
            "import os, sys\n"
            "state, work, work_site, run_dir = sys.argv[1:5]\n"
            "for path in (state, work, work_site, run_dir):\n"
            "    if not os.access(path, os.X_OK):\n"
            "        print('NO_TRAVERSE ' + path)\n"
            "        sys.exit(1)\n"
            "try:\n"
            "    open(os.path.join(state, 'escaped-write'), 'w').close()\n"
            "except OSError:\n"
            "    print('STATE_NOT_WRITABLE')\n"
            "else:\n"
            "    print('STATE_WRITABLE')\n"
            "    sys.exit(2)\n"
            "with open(os.path.join(run_dir, 'build-probe'), 'w') as fh:\n"
            "    fh.write('ok\\n')\n"
            "print('WORKSPACE_WRITABLE')\n"
        )
        record = pwd.getpwnam(self.build_user)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                probe,
                str(self.state),
                str(self.state / "work"),
                str(work_site),
                str(run_dir),
            ],
            capture_output=True,
            text=True,
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            user=record.pw_uid,
            group=record.pw_gid,
        )
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertIn("STATE_NOT_WRITABLE", child.stdout)
        self.assertIn("WORKSPACE_WRITABLE", child.stdout)
        self.assertEqual((run_dir / "build-probe").read_text(), "ok\n")

    def test_commit_not_on_main_fails_before_build(self):
        self.seed_release("20260101T000001Z")
        result = self.run_deploy(env_extra={"YRDS_TEST_ANCESTOR": "no"})
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), ["20260101T000001Z"])
        self.assertEqual(self.npm_calls(), [])

    def test_live_commit_is_an_idempotent_noop(self):
        release = self.seed_release("20260101T000001Z", commit=self.commit)
        before = self.current_target()
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("noop", result.stdout)
        self.assertEqual(self.release_names(), ["20260101T000001Z"])
        self.assertEqual(self.current_target(), before)
        git_log = (self.root / "git.log")
        self.assertFalse(git_log.exists() and "archive" in git_log.read_text())
        # a missing compatibility record is repaired, not skipped
        compat = self.www / "RELEASE-METADATA.txt"
        self.assertTrue(compat.is_file())
        self.assertEqual(
            compat.read_text(), (release / "RELEASE-METADATA.txt").read_text()
        )

    def test_build_failure_leaves_current_untouched(self):
        self.seed_release("20260101T000001Z")
        before = self.current_target()
        result = self.run_deploy(env_extra={"YRDS_TEST_BUILD_FAIL": "1"})
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), ["20260101T000001Z"])
        self.assertEqual(self.current_target(), before)

    def test_pre_activation_nginx_failure_creates_no_release(self):
        self.seed_release("20260101T000001Z")
        before = self.current_target()
        result = self.run_deploy(env_extra={"YRDS_TEST_NGINX_T_FAIL_AT": "1"})
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), ["20260101T000001Z"])
        self.assertEqual(self.current_target(), before)

    def test_post_swap_nginx_failure_restores_previous(self):
        self.seed_release("20260101T000001Z")
        result = self.run_deploy(env_extra={"YRDS_TEST_NGINX_T_FAIL_AT": "2"})
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        self.assertEqual(self.current_target(), "releases/20260101T000001Z")
        # the failed release stays on disk for evidence, but is not current
        self.assertEqual(len(self.release_names()), 2)
        self.assertIn("rolling back", result.stdout)

    def test_smoke_body_mismatch_restores_previous(self):
        self.seed_release("20260101T000001Z")
        wrong = self.root / "wrong-body"
        wrong.write_text("<html>not the built body</html>")
        result = self.run_deploy(
            env_extra={"YRDS_TEST_SMOKE_BODY_FILE": str(wrong)}
        )
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        self.assertEqual(self.current_target(), "releases/20260101T000001Z")
        curl_log = (self.root / "curl.log").read_text()
        self.assertIn("--resolve bau.example.test:443:127.0.0.1", curl_log)

    def test_reload_failure_restores_previous(self):
        self.seed_release("20260101T000001Z")
        result = self.run_deploy(env_extra={"YRDS_TEST_NGINX_RELOAD_FAIL": "1"})
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(self.current_target(), "releases/20260101T000001Z")

    def test_successful_deploy_swaps_prunes_and_records(self):
        for index in range(1, 6):
            self.seed_release("20260101T00000%dZ" % index, activate=(index == 5))
        stray = self.releases / "manual-not-a-release"
        stray.mkdir()
        (stray / "keep.txt").write_text("keep\n")
        smoke_body = self.root / "smoke-body"
        smoke_body.write_text("<html>built</html>")
        result = self.run_deploy(
            "--run-id",
            "test-success-1",
            env_extra={"YRDS_TEST_SMOKE_BODY_FILE": str(smoke_body)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = self.current_target()
        self.assertTrue(target and TS_RE.match(Path(target).name), target)
        names = self.release_names()
        self.assertEqual(len(names), 3, names)
        self.assertIn(Path(target).name, names)
        self.assertIn("20260101T000005Z", names)  # newest 3 kept
        self.assertIn("20260101T000004Z", names)
        self.assertTrue(stray.is_dir(), "prune must never delete non-release entries")
        new_release = self.www / target
        meta = (new_release / "RELEASE-METADATA.txt").read_text()
        self.assertIn("commit: %s" % self.commit, meta)
        self.assertIn("source: manual", meta)
        self.assertIn("activation-utc:", meta)
        compat = (self.www / "RELEASE-METADATA.txt").read_text()
        self.assertEqual(compat, meta)
        self.assertEqual(
            (new_release / "index.html").read_text(), "<html>built</html>"
        )
        # unprivileged build, capped environment, private HOME/cache per run
        calls = self.npm_calls()
        self.assertEqual([call["args"][0] for call in calls], ["ci", "run"])
        for call in calls:
            self.assertEqual(call["CI"], "1")
            self.assertEqual(call["NODE_OPTIONS"], "--max-old-space-size=512")
            self.assertTrue(call["HOME"].startswith(str(self.state / "work" / "bau")))
            self.assertTrue(call["npm_config_cache"].startswith(str(self.state / "work")))
        runuser_log = (self.root / "runuser.log").read_text()
        self.assertIn("runuser -u %s --" % self.build_user, runuser_log)
        # pinned root-only deploy key with strict host checking
        git_log = (self.root / "git.log").read_text()
        self.assertIn("-i %s" % self.deploy_key, git_log)
        self.assertIn("IdentitiesOnly=yes", git_log)
        self.assertIn("StrictHostKeyChecking=yes", git_log)
        self.assertIn("UserKnownHostsFile=%s" % self.known_hosts, git_log)
        self.assertIn("merge-base --is-ancestor %s refs/heads/main" % self.commit, git_log)
        # per-run log exists and stays clean
        log_file = self.logs / "test-success-1.log"
        self.assertTrue(log_file.is_file())
        log_text = log_file.read_text() + result.stdout + result.stderr
        self.assertNotIn(self.secret_sentinel, log_text)
        self.assertNotIn("SENTINEL-KEY-MATERIAL", log_text)
        self.assertNotIn("PRIVATE KEY", log_text)

    def test_compat_refresh_failure_keeps_verified_release_live(self):
        self.seed_release("20260101T000001Z")
        compat = self.www / "RELEASE-METADATA.txt"
        compat.mkdir()  # a non-empty directory can never be replaced by the file
        (compat / "blocker").write_text("x\n")
        smoke_body = self.root / "smoke-body"
        smoke_body.write_text("<html>built</html>")
        result = self.run_deploy(
            "--run-id",
            "test-compat-fail",
            env_extra={"YRDS_TEST_SMOKE_BODY_FILE": str(smoke_body)},
        )
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        target = self.current_target()
        self.assertTrue(target and TS_RE.match(Path(target).name), target)
        self.assertNotEqual(target, "releases/20260101T000001Z")
        self.assertTrue((self.www / target / "index.html").is_file())
        self.assertIn("compatibility", result.stdout)
        self.assertEqual(len(self.release_names()), 2)  # no prune on this path

    def test_retry_repairs_stale_compat_metadata(self):
        release = self.seed_release("20260101T000001Z", commit=self.commit)
        compat = self.www / "RELEASE-METADATA.txt"
        compat.write_text("stale record\n")
        before = self.current_target()
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("noop", result.stdout)
        self.assertIn("repaired", result.stdout)
        self.assertEqual(self.current_target(), before)
        self.assertEqual(
            compat.read_text(), (release / "RELEASE-METADATA.txt").read_text()
        )
        self.assertEqual(self.release_names(), ["20260101T000001Z"])

    def test_build_only_measures_without_release(self):
        self.seed_release("20260101T000001Z")
        before = self.current_target()
        result = self.run_deploy(
            "--build-only", "--run-id", "test-buildonly", source="manual-test"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), ["20260101T000001Z"])
        self.assertEqual(self.current_target(), before)
        self.assertIn("build-only", result.stdout)
        self.assertIn("measure label=build", result.stdout)
        self.assertIn("disk_free_bytes=", result.stdout)
        calls = self.npm_calls()
        self.assertTrue(calls)
        self.assertTrue(
            calls[0]["cwd"].startswith(
                str(self.state / "work" / "bau" / "test-buildonly")
            )
        )

    def test_simulate_build_failure_proves_current_unchanged(self):
        self.seed_release("20260101T000001Z")
        before = self.current_target()
        result = self.run_deploy("--simulate-build-failure", source="manual-test")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), ["20260101T000001Z"])
        self.assertEqual(self.current_target(), before)
        self.assertEqual(self.npm_calls(), [])

    def test_stale_work_cleanup(self):
        old = self.state / "work" / "bau" / "old-run"
        old.mkdir(parents=True)
        (old / "leftover.txt").write_text("old\n")
        past = time.time() - 100 * 3600
        os.utime(old, (past, past))
        fresh = self.state / "work" / "bau" / "fresh-run"
        fresh.mkdir()
        result = self.run_deploy("--run-id", "test-stale")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists(), "workspace older than 72h must be removed")
        self.assertTrue(fresh.exists(), "fresh workspace must survive")

    def test_missing_build_user_is_a_config_error(self):
        cfg_path = self.config_dir / "bau.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["build_user"] = "no-such-user-xyz"
        cfg_path.write_text(json.dumps(cfg))
        result = self.run_deploy()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.release_names(), [])


class InstallerDriftTests(unittest.TestCase):
    """Installer owner/mode drift repair and pinned known_hosts (rerun safety)."""

    def setUp(self):
        if os.geteuid() != 0:
            raise unittest.SkipTest("installer drift tests chown root-owned files")
        self.root = Path(tempfile.mkdtemp(prefix="yrds-install-test-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_secret_mode_is_enforced_on_rerun(self):
        secret = self.root / "bau.webhook"
        secret.write_text("placeholder\n")
        secret.chmod(0o644)
        self.assertFalse(installer.ensure_secret(secret, "drift secret"))
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o400)
        self.assertEqual(secret.stat().st_uid, 0)
        self.assertTrue(secret.read_text().strip())  # value preserved for re-reads
        self.assertFalse(installer.ensure_secret(secret, "drift secret"))
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o400)

    def test_deploy_key_mode_is_enforced_without_regeneration(self):
        key = self.root / "bau_ed25519"
        key.write_text("placeholder key\n")
        key.chmod(0o644)
        before = key.read_text()
        installer.ensure_deploy_key(key, "bau")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o400)
        self.assertEqual(key.read_text(), before)

    def test_known_hosts_drift_is_replaced_with_pinned_key(self):
        known = self.root / "known_hosts"
        known.write_text("attacker.example ssh-ed25519 AAAAstub\n")
        self.assertTrue(
            installer.ensure_known_hosts(known, installer.EXPECTED_BITBUCKET_ED25519)
        )
        text = known.read_text()
        self.assertNotIn("attacker.example", text)
        self.assertIn("bitbucket.org ssh-ed25519", text)
        self.assertTrue(
            installer.known_hosts_matches(known, installer.EXPECTED_BITBUCKET_ED25519)
        )
        self.assertEqual(stat.S_IMODE(known.stat().st_mode), 0o644)
        # rerun with correct content: kept, idempotent
        self.assertFalse(
            installer.ensure_known_hosts(known, installer.EXPECTED_BITBUCKET_ED25519)
        )

    def test_known_hosts_with_an_extra_key_is_replaced(self):
        known = self.root / "known_hosts"
        blob = base64.b64encode(b"ssh-ed25519" + b"\x01" * 32).decode()
        known.write_text(
            installer.BITBUCKET_ED25519_PINNED
            + "\n"
            + "extra.example ssh-ed25519 %s\n" % blob
        )
        installer.ensure_known_hosts(known, installer.EXPECTED_BITBUCKET_ED25519)
        self.assertNotIn("extra.example", known.read_text())

    def test_ensure_dir_repairs_drifted_mode(self):
        drifted = self.root / "secrets"
        drifted.mkdir()
        drifted.chmod(0o777)
        self.assertTrue(installer.ensure_dir(drifted, 0o700))
        self.assertEqual(stat.S_IMODE(drifted.stat().st_mode), 0o700)
        self.assertEqual(drifted.stat().st_uid, 0)

    def test_pinned_fingerprint_matches_published_value(self):
        self.assertEqual(
            installer.key_blob_fingerprint(installer.BITBUCKET_ED25519_PINNED),
            installer.EXPECTED_BITBUCKET_ED25519,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
