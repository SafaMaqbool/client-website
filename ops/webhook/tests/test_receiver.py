#!/usr/bin/env python3
"""Hermetic tests for ops/webhook/bin/receiver.py.

Everything runs from a temporary root with a command stub standing in for
deploy-site: no production paths, no network, no secrets beyond the
throwaway value created in the test's own temp dir.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib import error as urlerror
from urllib import request as urlrequest

TESTS_DIR = Path(__file__).resolve().parent
BIN = TESTS_DIR.parent / "bin"


def load_receiver():
    spec = importlib.util.spec_from_file_location("yrds_receiver", BIN / "receiver.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


receiver = load_receiver()

# Atlassian's published verification vector for Bitbucket Cloud webhooks
# (support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/):
#   secret  It's a Secret to Everybody
#   payload Hello World!
#   header  sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9
ATLASSIAN_SECRET = b"It's a Secret to Everybody"
ATLASSIAN_BODY = b"Hello World!"
ATLASSIAN_SIG = (
    "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"
)

DEPLOY_STUB = """#!/usr/bin/env python3
import os, sys, time
log = os.environ["YRDS_TEST_DEPLOY_LOG"]
with open(log, "a") as fh:
    fh.write("start %s %.6f\\n" % (" ".join(sys.argv[1:]), time.time()))
time.sleep(float(os.environ.get("YRDS_TEST_DEPLOY_SLEEP", "0")))
code = int(os.environ.get("YRDS_TEST_DEPLOY_EXIT", "0"))
with open(log, "a") as fh:
    fh.write("end %d %.6f\\n" % (code, time.time()))
sys.exit(code)
"""


def wait_for(predicate, timeout=20.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class ReceiverTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yrds-receiver-test-"))
        self.config_dir = self.tmp / "sites.d"
        self.config_dir.mkdir()
        self.state_dir = self.tmp / "state"
        self.log_dir = self.tmp / "logs"
        self.log_dir.mkdir()
        self.secret = b"unit-test-webhook-secret-0123456789"
        secret_file = self.tmp / "bau.webhook"
        secret_file.write_bytes(self.secret + b"\n")
        self.deploy_log = self.tmp / "deploy.log"
        deploy_stub = self.tmp / "deploy-stub"
        deploy_stub.write_text(DEPLOY_STUB)
        deploy_stub.chmod(0o755)
        self.kill_switch = self.tmp / "DISABLED"
        site = {
            "site": "bau",
            "repo_full_name": "uhpsoftware/7yrds-bau",
            "branch": "main",
            "secret_file": str(secret_file),
            "state_dir": str(self.state_dir),
            "releases_dir": str(self.tmp / "releases"),
            "current_link": str(self.tmp / "current"),
            "log_dir": str(self.log_dir),
            "lock_file": str(self.tmp / "bau.lock"),
            "deploy_command": str(deploy_stub),
            "kill_switch": str(self.kill_switch),
        }
        (self.config_dir / "bau.json").write_text(json.dumps(site))
        self._env_backup = dict(os.environ)
        os.environ["YRDS_TEST_DEPLOY_LOG"] = str(self.deploy_log)
        self.app = receiver.ReceiverApp(self.config_dir, self.state_dir, "127.0.0.1", 0)
        host, port = self.app.start()
        self.base = "http://127.0.0.1:%d" % port
        self.log_stream = io.StringIO()
        self._log_handler = logging.StreamHandler(self.log_stream)
        self._log_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        receiver.LOG.addHandler(self._log_handler)
        self._old_level = receiver.LOG.level
        receiver.LOG.setLevel(logging.INFO)

    def tearDown(self):
        receiver.LOG.removeHandler(self._log_handler)
        receiver.LOG.setLevel(self._old_level)
        self.app.stop()
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------
    def post(self, body, headers=None, path="/__deploy/hooks/bitbucket/bau", method="POST"):
        req = urlrequest.Request(self.base + path, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urlrequest.urlopen(req, timeout=15) as response:
                payload = response.read().decode("utf-8") or "{}"
                return response.status, json.loads(payload), dict(response.headers)
        except urlerror.HTTPError as err:
            payload = err.read().decode("utf-8") or "{}"
            return err.code, json.loads(payload), dict(err.headers)

    def sig_headers(self, body, secret=None, event="repo:push", request_uuid=None):
        signature = "sha256=" + hmac.new(
            secret if secret is not None else self.secret, body, hashlib.sha256
        ).hexdigest()
        headers = {"X-Hub-Signature": signature, "X-Event-Key": event}
        if request_uuid:
            headers["X-Request-UUID"] = request_uuid
        return headers

    @staticmethod
    def payload(commit="a" * 40, branch="main", full_name="uhpsoftware/7yrds-bau",
                changes=None):
        if changes is None:
            changes = [
                {
                    "new": {
                        "type": "branch",
                        "name": branch,
                        "target": {"hash": commit},
                    }
                }
            ]
        doc = {"repository": {"full_name": full_name}, "push": {"changes": changes}}
        return json.dumps(doc).encode("utf-8")

    def queue_rows(self, status=None):
        db = self.state_dir / "queue.db"
        if not db.exists():
            return []
        conn = sqlite3.connect(db)
        if status:
            rows = conn.execute("SELECT site, commit_sha, status FROM queue WHERE status=?", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT site, commit_sha, status FROM queue").fetchall()
        conn.close()
        return rows

    def deploy_starts(self):
        if not self.deploy_log.exists():
            return []
        return [
            line for line in self.deploy_log.read_text().splitlines() if line.startswith("start ")
        ]

    # ---- signature -------------------------------------------------------
    def test_atlassian_hmac_vector(self):
        self.assertTrue(receiver.verify_signature(ATLASSIAN_SECRET, ATLASSIAN_BODY, ATLASSIAN_SIG))
        # any mutation of the signature must fail
        tampered = "sha256=" + "0" * 64
        self.assertFalse(receiver.verify_signature(ATLASSIAN_SECRET, ATLASSIAN_BODY, tampered))
        self.assertFalse(
            receiver.verify_signature(ATLASSIAN_SECRET, ATLASSIAN_BODY + b"!", ATLASSIAN_SIG)
        )

    def test_bad_signature_returns_401_before_json(self):
        body = b"{not even json"
        # signature present but wrong -> 401 (not 400): HMAC is checked first
        status, payload, _ = self.post(body, self.sig_headers(body, secret=b"wrong"))
        self.assertEqual(status, 401, payload)
        # missing signature header
        status, _, _ = self.post(body, {"X-Event-Key": "repo:push"})
        self.assertEqual(status, 401)
        # malformed signature header
        status, _, _ = self.post(body, {"X-Hub-Signature": "md5=abc", "X-Event-Key": "repo:push"})
        self.assertEqual(status, 401)
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.deploy_starts(), [])

    def test_non_post_returns_405_with_allow(self):
        for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS"):
            status, _, headers = self.post(b"", None, method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers.get("Allow"), "POST")

    def test_wrong_event_and_repository(self):
        body = self.payload()
        status, payload, _ = self.post(body, self.sig_headers(body, event="pullrequest:created"))
        self.assertEqual(status, 400, payload)
        body = self.payload(full_name="someone/else")
        status, payload, _ = self.post(body, self.sig_headers(body))
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.deploy_starts(), [])

    def test_wrong_branch_and_deletion_are_ignored_without_job(self):
        body = self.payload(branch="feature/x")
        status, payload, _ = self.post(body, self.sig_headers(body, request_uuid="u-1"))
        self.assertEqual((status, payload.get("result")), (202, "ignored"))
        deletion = [
            {
                "new": None,
                "old": {"type": "branch", "name": "main", "target": {"hash": "c" * 40}},
                "closed": True,
            }
        ]
        body = self.payload(changes=deletion)
        status, payload, _ = self.post(body, self.sig_headers(body, request_uuid="u-2"))
        self.assertEqual((status, payload.get("result")), (202, "ignored"))
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.deploy_starts(), [])

    def test_malformed_json_and_commit(self):
        body = b"{not json"
        status, _, _ = self.post(body, self.sig_headers(body, request_uuid="u-j"))
        self.assertEqual(status, 400)
        body = self.payload(commit="zzzz")
        status, _, _ = self.post(body, self.sig_headers(body, request_uuid="u-c"))
        self.assertEqual(status, 400)
        self.assertEqual(self.queue_rows(), [])

    # ---- queue / worker --------------------------------------------------
    def test_main_push_enqueues_and_runs_once(self):
        commit = "1" * 40
        body = self.payload(commit=commit)
        status, payload, _ = self.post(body, self.sig_headers(body, request_uuid="uuid-run-1"))
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload.get("result"), "queued")
        finished = wait_for(lambda: [r for r in self.queue_rows() if r[2] == "done"])
        self.assertTrue(finished, "job did not complete")
        starts = self.deploy_starts()
        self.assertEqual(len(starts), 1, starts)
        self.assertIn("--source webhook", starts[0])
        self.assertIn("--no-lock", starts[0])
        self.assertIn("--commit " + commit, starts[0])
        self.assertIn("uuid-run-1", starts[0])

    def test_duplicate_delivery_runs_no_second_build(self):
        os.environ["YRDS_TEST_DEPLOY_SLEEP"] = "0.6"
        commit = "2" * 40
        body = self.payload(commit=commit)
        headers = self.sig_headers(body, request_uuid="uuid-dup-1")
        status1, payload1, _ = self.post(body, headers)
        status2, payload2, _ = self.post(body, headers)  # retry while queued/running
        self.assertEqual(status1, 202)
        self.assertEqual(payload1.get("result"), "queued")
        self.assertEqual(status2, 202)
        self.assertEqual(payload2.get("result"), "duplicate")
        finished = wait_for(lambda: [r for r in self.queue_rows() if r[2] == "done"])
        self.assertTrue(finished, "job did not complete")
        status3, payload3, _ = self.post(body, headers)  # retry after completion
        self.assertEqual((status3, payload3.get("result")), (202, "duplicate"))
        time.sleep(0.3)
        self.assertEqual(len(self.deploy_starts()), 1, self.deploy_starts())

    def test_worker_serializes_jobs(self):
        os.environ["YRDS_TEST_DEPLOY_SLEEP"] = "0.5"
        for index in (3, 4):
            body = self.payload(commit=str(index) * 40)
            status, payload, _ = self.post(
                body, self.sig_headers(body, request_uuid="uuid-ser-%d" % index)
            )
            self.assertEqual(status, 202, payload)
        done = wait_for(
            lambda: len([r for r in self.queue_rows() if r[2] == "done"]) == 2, timeout=30
        )
        self.assertTrue(done, "both jobs did not complete")
        intervals = []
        for line in self.deploy_log.read_text().splitlines():
            kind, _, rest = line.partition(" ")
            timestamp = float(rest.rsplit(" ", 1)[1])
            intervals.append((kind, timestamp))
        starts = [ts for kind, ts in intervals if kind == "start"]
        ends = [ts for kind, ts in intervals if kind == "end"]
        self.assertEqual(len(starts), 2, intervals)
        self.assertEqual(len(ends), 2, intervals)
        self.assertGreaterEqual(starts[1], ends[0], "deploys overlapped: %r" % intervals)

    def test_kill_switch_returns_503_without_enqueue(self):
        self.kill_switch.write_text("disabled\n")
        body = self.payload()
        status, payload, _ = self.post(body, self.sig_headers(body, request_uuid="uuid-kill"))
        self.assertEqual(status, 503, payload)
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.deploy_starts(), [])

    def test_body_too_large_returns_413(self):
        body = b"x" * (receiver.MAX_BODY_BYTES + 16)
        status, payload, _ = self.post(body, self.sig_headers(body, request_uuid="uuid-big"))
        self.assertEqual(status, 413, payload)
        self.assertEqual(self.queue_rows(), [])

    def test_unknown_path_and_site_404(self):
        body = self.payload()
        status, _, _ = self.post(body, self.sig_headers(body), path="/nope")
        self.assertEqual(status, 404)
        status, _, _ = self.post(body, self.sig_headers(body), path="/__deploy/hooks/bitbucket/other")
        self.assertEqual(status, 404)

    def test_recover_running_resets_to_queued(self):
        db = self.state_dir / "queue.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO queue (site, commit_sha, request_uuid, run_id, status, created_utc)"
            " VALUES ('bau', ?, 'uuid-crash', 'uuid-crash', 'running', 't')",
            ("9" * 40,),
        )
        conn.commit()
        conn.close()
        store = receiver.QueueStore(db)
        self.assertEqual(store.recover_running(), 1)
        self.assertEqual(len(self.queue_rows(status="queued")), 1)

    def test_logs_never_contain_body_or_secret(self):
        commit = "5" * 40
        body = self.payload(commit=commit)
        status, _, _ = self.post(body, self.sig_headers(body, request_uuid="uuid-log-1"))
        self.assertEqual(status, 202)
        wait_for(lambda: [r for r in self.queue_rows() if r[2] == "done"])
        captured = self.log_stream.getvalue()
        self.assertNotIn(self.secret.decode(), captured)
        self.assertNotIn("Hello World!", captured)
        self.assertNotIn(body.decode(), captured)
        self.assertNotIn("X-Hub-Signature", captured)

    # ---- regressions (review 2026-09-29): ids, nesting, modes, ordering ---
    def test_run_id_validation_rejects_dot_components(self):
        for value in (".", ".."):
            self.assertFalse(receiver.valid_run_id(value), value)
        for value in ("", None, "a/b", "a b", "x" * 65):
            self.assertFalse(receiver.valid_run_id(value), repr(value))
        for value in ("ok-1", "a.b_c", "run.2026-09-29"):
            self.assertTrue(receiver.valid_run_id(value), value)
        self.assertNotIn(receiver.safe_run_id("."), (".", ".."))
        self.assertRegex(receiver.safe_run_id(".."), r"^[0-9a-f]{32}$")
        self.assertEqual(receiver.safe_run_id("ok-1"), "ok-1")

    def test_missing_or_malformed_request_uuid_is_rejected(self):
        commit = "7" * 40
        body = self.payload(commit=commit)
        for raw in (None, "", ".", "..", "bad/uuid", "x" * 65):
            status, payload, _ = self.post(
                body, self.sig_headers(body, request_uuid=raw)
            )
            self.assertEqual(status, 400, (raw, payload))
            self.assertEqual(payload.get("result"), "invalid request uuid", raw)
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.deploy_starts(), [])
        status, payload, _ = self.post(
            body, self.sig_headers(body, request_uuid="ok.uuid-1")
        )
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload.get("result"), "queued")
        finished = wait_for(lambda: [r for r in self.queue_rows() if r[2] == "done"])
        self.assertTrue(finished, "valid delivery did not complete")

    def test_malformed_signed_nested_structures_return_4xx(self):
        body = self.payload(
            changes=[
                {"new": {"type": "branch", "name": "main", "target": "not-an-object"}}
            ]
        )
        status, payload, _ = self.post(
            body, self.sig_headers(body, request_uuid="u-bad-1")
        )
        self.assertEqual(status, 400, payload)
        body = self.payload(
            changes=[{"new": {"type": "branch", "name": "main", "target": {}}}]
        )
        status, payload, _ = self.post(
            body, self.sig_headers(body, request_uuid="u-bad-2")
        )
        self.assertEqual(status, 400, payload)
        body = json.dumps(
            {"repository": "uhpsoftware/7yrds-bau", "push": {"changes": []}}
        ).encode("utf-8")
        status, payload, _ = self.post(
            body, self.sig_headers(body, request_uuid="u-bad-3")
        )
        self.assertEqual(status, 403, payload)
        for doc in (
            {"repository": {"full_name": "uhpsoftware/7yrds-bau"}, "push": "x"},
            {
                "repository": {"full_name": "uhpsoftware/7yrds-bau"},
                "push": {"changes": {"a": 1}},
            },
            {
                "repository": {"full_name": "uhpsoftware/7yrds-bau"},
                "push": {"changes": ["x"]},
            },
        ):
            body = json.dumps(doc).encode("utf-8")
            status, payload, _ = self.post(
                body, self.sig_headers(body, request_uuid="u-bad-4")
            )
            self.assertEqual((status, payload.get("result")), (202, "ignored"), doc)
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.deploy_starts(), [])
        self.assertFalse(
            receiver._Handler._target_commit(
                {
                    "push": {
                        "changes": [
                            {
                                "new": {
                                    "type": "branch",
                                    "name": "main",
                                    "target": "nope",
                                }
                            }
                        ]
                    }
                },
                "main",
            )
        )

    def test_queue_state_files_are_root_only(self):
        db = self.state_dir / "queue.db"
        self.assertTrue(db.is_file())
        self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600, db)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o711)
        # Real creation modes under a permissive umask.  A connection with an
        # engaged WAL read transaction keeps -wal/-shm present while the store
        # writes and re-secures them.
        old_umask = os.umask(0o022)
        store = receiver.QueueStore(db)
        holder = sqlite3.connect(db)
        try:
            holder.execute("BEGIN")
            holder.execute("SELECT count(*) FROM deliveries").fetchone()
            store.enqueue("bau", "uuid-perm", "e" * 40, "uuid-perm")
            for suffix in ("-wal", "-shm"):
                path = Path(str(db) + suffix)
                self.assertTrue(path.exists(), path)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        finally:
            holder.rollback()
            holder.close()
            os.umask(old_umask)

    def test_state_dir_is_traversable_but_not_writable_by_build_user(self):
        """Traversal regression (2026-09-29): the state dir must be crossable
        by the unprivileged build user so it can reach work/, while staying
        root-owned, unlistable and unwritable; only traversal bits are given.
        Fails on the old 0o700 behaviour (build workspace unreachable)."""
        mode = stat.S_IMODE(self.state_dir.stat().st_mode)
        self.assertEqual(mode, 0o711, oct(mode))
        self.assertEqual(self.state_dir.stat().st_uid, 0)
        self.assertEqual(mode & 0o022, 0, "must be group/other unwritable")
        self.assertEqual(mode & 0o001, 0o001, "build user needs x to traverse")
        self.assertEqual(mode & 0o004, 0, "build user must not be able to list")

    def test_durable_enqueue_before_202_and_no_wait_for_work(self):
        os.environ["YRDS_TEST_DEPLOY_SLEEP"] = "2.5"
        commit = "6" * 40
        body = self.payload(commit=commit)
        started = time.time()
        status, payload, _ = self.post(
            body, self.sig_headers(body, request_uuid="uuid-order-1")
        )
        elapsed = time.time() - started
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload.get("result"), "queued")
        self.assertLess(elapsed, 1.5, "the 202 was delayed by build work")
        conn = sqlite3.connect(self.state_dir / "queue.db")
        row = conn.execute(
            "SELECT status FROM deliveries"
            " WHERE site='bau' AND request_uuid=? AND commit_sha=?",
            ("uuid-order-1", commit),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "enqueue was not durable before the 202")
        self.assertIn(row[0], ("queued", "running"))
        self.assertTrue(wait_for(lambda: self.deploy_starts(), timeout=10))
        ends = [
            line
            for line in self.deploy_log.read_text().splitlines()
            if line.startswith("end ")
        ]
        self.assertEqual(
            ends, [], "deploy finished before the response could be checked"
        )
        body2 = self.payload(commit="8" * 40)
        started2 = time.time()
        status2, payload2, _ = self.post(
            body2, self.sig_headers(body2, request_uuid="uuid-order-2")
        )
        self.assertLess(time.time() - started2, 1.5)
        self.assertEqual((status2, payload2.get("result")), (202, "queued"))
        done = wait_for(
            lambda: len([r for r in self.queue_rows() if r[2] == "done"]) == 2,
            timeout=30,
        )
        self.assertTrue(done, "queued jobs did not finish")

    # ---- regressions: worker containment / watchdog ----------------------
    def test_worker_launch_failure_is_contained_and_marked(self):
        store = receiver.QueueStore(self.state_dir / "worker-probe.db")
        cfg = SimpleNamespace(
            lock_file=str(self.tmp / "probe.lock"),
            deploy_command=str(self.tmp / "missing-deploy-site"),
        )
        app = SimpleNamespace(store=store, sites={"bau": cfg})
        store.enqueue("bau", "uuid-launch-fail", "f" * 40, "uuid-launch-fail")
        job = store.claim_next()
        self.assertIsNotNone(job)
        worker = receiver.DeployWorker(app)
        worker._process(job)  # must not raise
        conn = sqlite3.connect(self.state_dir / "worker-probe.db")
        queue_status = conn.execute(
            "SELECT status FROM queue WHERE id=?", (job["queue_id"],)
        ).fetchone()[0]
        delivery = conn.execute(
            "SELECT status, detail FROM deliveries"
            " WHERE site=? AND request_uuid=? AND commit_sha=?",
            (job["site"], job["request_uuid"], job["commit_sha"]),
        ).fetchone()
        conn.close()
        self.assertEqual(queue_status, "failed")
        self.assertEqual(delivery[0], "failed")
        self.assertIn("cannot launch", delivery[1])

    def test_worker_survives_store_exceptions_and_records_health(self):
        class FlakyStore:
            def __init__(self):
                self.calls = 0

            def claim_next(self):
                self.calls += 1
                if self.calls <= 2:
                    raise sqlite3.OperationalError("database is locked")
                return None

            def finish(self, job, ok, detail):  # pragma: no cover
                raise AssertionError("finish must not be called")

        app = SimpleNamespace(store=FlakyStore(), sites={})
        worker = receiver.DeployWorker(app)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(
                wait_for(lambda: app.store.calls >= 3, timeout=15),
                "worker did not keep polling after store errors",
            )
        finally:
            worker.stop()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(worker.errors, 2)
        self.assertIn("claim_next failed", worker.last_error)

    def test_watchdog_restarts_a_dead_worker(self):
        app = receiver.ReceiverApp(
            self.config_dir, self.tmp / "supervised-state", "127.0.0.1", 0
        )

        class DeadWorker:
            errors = 3

            def is_alive(self):
                return False

            def is_stopping(self):
                return False

            def stop(self):
                pass

        app.worker = DeadWorker()
        self.assertTrue(app.supervise_worker_once())
        self.assertIsInstance(app.worker, receiver.DeployWorker)
        self.assertTrue(app.worker.is_alive())
        restarted = app.worker
        self.assertFalse(app.supervise_worker_once())
        stopping = receiver.DeployWorker(app)
        stopping.stop()
        app.worker = stopping
        self.assertFalse(app.supervise_worker_once())
        restarted.stop()
        restarted.join(timeout=5)
        app.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
