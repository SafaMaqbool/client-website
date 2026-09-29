#!/usr/bin/env python3
"""7YRDS production deploy webhook receiver.

Binding design: /opt/data/wf/t_7ccb4522/design.md (TRIAGE-7).

Stdlib-only HTTP receiver for the Bitbucket ``repo:push`` webhook of the
7YRDS sites.  It verifies the HMAC signature *before* parsing JSON, filters
event / repository / branch, dedupes deliveries durably in SQLite, and hands
accepted work to exactly one asynchronous worker that runs the root-only
``deploy-site`` pipeline under the per-site ``flock``.

Logs carry event ids, commits and statuses only -- never request bodies,
signature headers, tokens, the webhook secret or private-key material.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("7yrds-webhook")

MAX_BODY_BYTES = 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SIG_RE = re.compile(r"^sha256=[0-9a-f]{64}$")
ENDPOINT_RE = re.compile(r"^/__deploy/hooks/bitbucket/([a-z0-9][a-z0-9-]*)$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REQUIRED_EVENT = "repo:push"
DEFAULT_CONFIG_DIR = "/etc/7yrds-deploy/sites.d"
DEFAULT_STATE_DIR = "/var/lib/7yrds-deploy"
DEFAULT_DEPLOY_COMMAND = "/opt/7yrds-deploy/bin/deploy-site"
DEFAULT_KILL_SWITCH = "/etc/7yrds-deploy/DISABLED"
WORKER_POLL_SECONDS = 0.2
WORKER_ERROR_BACKOFF_SECONDS = 1.0
WORKER_SUPERVISE_SECONDS = 2.0
# Traverse-only: the unprivileged build user must be able to cross the state
# dir to reach its workspace under work/, but must never list or write in it.
STATE_DIR_MODE = 0o711
STATE_FILE_MODE = 0o600

DEDUPE_TERMINAL = ("queued", "running", "completed")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_run_id(value) -> bool:
    """True only for a bounded, filename-safe id that is not a dot path component."""
    if not isinstance(value, str) or not RUN_ID_RE.match(value):
        return False
    return value not in (".", "..")


def safe_run_id(value):
    """Return a filename-safe id; a raw header never becomes a pathname.

    Missing, malformed or dot-component values are replaced with a generated
    id (defense in depth -- the HTTP handler rejects such deliveries with a
    controlled 400 before any enqueue).
    """
    if valid_run_id(value):
        return value
    return uuid.uuid4().hex


def verify_signature(secret: bytes, body: bytes, header) -> bool:
    """True only for a well-formed, correct ``sha256=<hex>`` HMAC-SHA256."""
    if not isinstance(header, str) or not SIG_RE.match(header):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class SiteConfig:
    """Non-secret per-site mapping plus the in-memory webhook secret."""

    _REQUIRED = (
        "site",
        "repo_full_name",
        "branch",
        "secret_file",
        "state_dir",
        "releases_dir",
        "current_link",
        "log_dir",
        "lock_file",
    )

    def __init__(self, raw: dict):
        missing = [key for key in self._REQUIRED if key not in raw]
        if missing:
            raise ValueError("site config missing keys: %s" % ", ".join(missing))
        self.raw = raw
        self.site = raw["site"]
        self.repo_full_name = raw["repo_full_name"]
        self.branch = raw["branch"]
        self.secret_file = raw["secret_file"]
        self.state_dir = raw["state_dir"]
        self.releases_dir = raw["releases_dir"]
        self.current_link = raw["current_link"]
        self.log_dir = raw["log_dir"]
        self.lock_file = raw["lock_file"]
        self.deploy_command = raw.get("deploy_command", DEFAULT_DEPLOY_COMMAND)
        self.kill_switch = raw.get("kill_switch", DEFAULT_KILL_SWITCH)
        self.secret = None

    def load_secret(self) -> bytes:
        secret = Path(self.secret_file).read_bytes().strip()
        if not secret:
            raise ValueError("empty webhook secret for site %s" % self.site)
        self.secret = secret
        return secret


class QueueStore:
    """Root-only SQLite queue + delivery dedupe, durable before the 202."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        self._secure_files()
        return conn

    def _secure_files(self):
        """Reconcile root-only 0600 on the queue db and its WAL/SHM sidecars."""
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            try:
                if os.path.exists(path):
                    os.chmod(path, STATE_FILE_MODE)
            except OSError as err:
                LOG.warning("cannot enforce %o on %s: %s", STATE_FILE_MODE, path, err)

    def _commit(self, conn):
        conn.execute("COMMIT")
        self._secure_files()

    def _init(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS deliveries ("
                    " site TEXT NOT NULL, request_uuid TEXT NOT NULL,"
                    " commit_sha TEXT NOT NULL, status TEXT NOT NULL,"
                    " run_id TEXT NOT NULL, detail TEXT,"
                    " created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,"
                    " PRIMARY KEY (site, request_uuid, commit_sha))"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS queue ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT NOT NULL,"
                    " commit_sha TEXT NOT NULL, request_uuid TEXT NOT NULL,"
                    " run_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',"
                    " created_utc TEXT NOT NULL)"
                )
                self._secure_files()
            finally:
                conn.close()

    def enqueue(self, site, request_uuid, commit, run_id) -> str:
        """Durably enqueue; returns 'queued' or 'duplicate'."""
        now = utcnow()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status FROM deliveries"
                    " WHERE site=? AND request_uuid=? AND commit_sha=?",
                    (site, request_uuid, commit),
                ).fetchone()
                if row and row[0] in DEDUPE_TERMINAL:
                    self._commit(conn)
                    return "duplicate"
                if row:
                    conn.execute(
                        "UPDATE deliveries SET status='queued', run_id=?, detail=NULL,"
                        " updated_utc=?"
                        " WHERE site=? AND request_uuid=? AND commit_sha=?",
                        (run_id, now, site, request_uuid, commit),
                    )
                else:
                    conn.execute(
                        "INSERT INTO deliveries"
                        " (site, request_uuid, commit_sha, status, run_id,"
                        "  created_utc, updated_utc)"
                        " VALUES (?,?,?,'queued',?,?,?)",
                        (site, request_uuid, commit, run_id, now, now),
                    )
                conn.execute(
                    "INSERT INTO queue"
                    " (site, commit_sha, request_uuid, run_id, status, created_utc)"
                    " VALUES (?,?,?,?,'queued',?)",
                    (site, commit, request_uuid, run_id, now),
                )
                self._commit(conn)
                return "queued"
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def claim_next(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT id, site, commit_sha, request_uuid, run_id FROM queue"
                    " WHERE status='queued' ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    self._commit(conn)
                    return None
                now = utcnow()
                conn.execute("UPDATE queue SET status='running' WHERE id=?", (row[0],))
                conn.execute(
                    "UPDATE deliveries SET status='running', updated_utc=?"
                    " WHERE site=? AND request_uuid=? AND commit_sha=?",
                    (now, row[1], row[3], row[2]),
                )
                self._commit(conn)
                return {
                    "queue_id": row[0],
                    "site": row[1],
                    "commit_sha": row[2],
                    "request_uuid": row[3],
                    "run_id": row[4],
                }
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def finish(self, job, ok: bool, detail: str):
        now = utcnow()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE queue SET status=? WHERE id=?",
                    ("done" if ok else "failed", job["queue_id"]),
                )
                conn.execute(
                    "UPDATE deliveries SET status=?, detail=?, updated_utc=?"
                    " WHERE site=? AND request_uuid=? AND commit_sha=?",
                    (
                        "completed" if ok else "failed",
                        detail,
                        now,
                        job["site"],
                        job["request_uuid"],
                        job["commit_sha"],
                    ),
                )
                self._secure_files()
            finally:
                conn.close()

    def recover_running(self) -> int:
        """Reset interrupted running rows back to queued (idempotent deploy)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("UPDATE queue SET status='queued' WHERE status='running'")
                count = cur.rowcount
                conn.execute(
                    "UPDATE deliveries SET status='queued', updated_utc=?"
                    " WHERE status='running'",
                    (utcnow(),),
                )
                self._commit(conn)
                return count
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()


class DeployWorker(threading.Thread):
    """One serial worker; holds the per-site flock for the whole deploy."""

    def __init__(self, app):
        super().__init__(name="7yrds-deploy-worker", daemon=True)
        self.app = app
        self._stop = threading.Event()
        self.errors = 0
        self.last_error = None

    def stop(self):
        self._stop.set()

    def is_stopping(self):
        return self._stop.is_set()

    def _note_error(self, message, err=None):
        """Count and remember a contained error (never raised on)."""
        self.errors += 1
        self.last_error = message if err is None else "%s: %s" % (message, err)

    def run(self):
        LOG.info("deploy worker started")
        while not self._stop.is_set():
            try:
                job = self.app.store.claim_next()
            except Exception as err:  # store failure: back off, keep serving
                self._note_error("claim_next failed", err)
                LOG.exception(
                    "worker: claim_next failed (errors=%d); backing off", self.errors
                )
                self._stop.wait(WORKER_ERROR_BACKOFF_SECONDS)
                continue
            if job is None:
                self._stop.wait(WORKER_POLL_SECONDS)
                continue
            try:
                self._process(job)
            except Exception as err:  # last resort: the loop must never die
                self._note_error(
                    "unhandled error for delivery=%s" % job.get("run_id"), err
                )
                LOG.exception(
                    "worker: unhandled error for delivery=%s (errors=%d)",
                    job.get("run_id"),
                    self.errors,
                )
                self._fail(job, "unhandled worker error: %s" % err)
        LOG.info("deploy worker stopped (errors=%d)", self.errors)

    def _fail(self, job, detail):
        """Mark a claimed job failed; must never raise (best effort only)."""
        try:
            self.app.store.finish(job, False, detail)
        except Exception:
            LOG.exception(
                "worker: cannot record failure for delivery=%s", job.get("run_id")
            )

    def _process(self, job):
        site = job["site"]
        cfg = self.app.sites.get(site)
        if cfg is None:
            LOG.error("site=%s delivery=%s: no site config", site, job["run_id"])
            self._fail(job, "no site config")
            return
        try:
            lock_path = Path(cfg.lock_file)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as err:
            LOG.error(
                "site=%s delivery=%s: cannot open lock file: %s",
                site,
                job["run_id"],
                err,
            )
            self._fail(job, "cannot open lock file: %s" % err)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._run_deploy(job, cfg)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _run_deploy(self, job, cfg):
        """One deploy-site run; every failure is recorded, never raised on."""
        site = job["site"]
        LOG.info(
            "site=%s commit=%s delivery=%s lock acquired, starting deploy",
            site,
            job["commit_sha"][:12],
            job["run_id"],
        )
        command = [
            cfg.deploy_command,
            "--site",
            site,
            "--commit",
            job["commit_sha"],
            "--source",
            "webhook",
            "--run-id",
            job["run_id"],
            "--no-lock",  # this worker already holds the per-site flock
        ]
        try:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as err:
            LOG.error(
                "site=%s delivery=%s: cannot launch deploy-site: %s",
                site,
                job["run_id"],
                err,
            )
            self._fail(job, "cannot launch deploy-site: %s" % err)
            return
        for line in (proc.stdout or "").splitlines():
            LOG.info("site=%s delivery=%s deploy: %s", site, job["run_id"], line)
        ok = proc.returncode == 0
        try:
            self.app.store.finish(job, ok, "exit=%d" % proc.returncode)
        except Exception as err:
            self._note_error("cannot record deploy result", err)
            LOG.exception(
                "site=%s delivery=%s: cannot record deploy result",
                site,
                job["run_id"],
            )
            self._fail(job, "cannot record deploy result: %s" % err)
            return
        LOG.info(
            "site=%s commit=%s delivery=%s finished status=%s exit=%d",
            site,
            job["commit_sha"][:12],
            job["run_id"],
            "ok" if ok else "failed",
            proc.returncode,
        )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "7yrds-webhook/1.0"

    @property
    def app(self):
        return self.server.app

    # ---- helpers ---------------------------------------------------------
    def log_message(self, fmt, *args):
        LOG.info("http client=%s %s", self.client_address[0], fmt % args)

    def _send_json(self, status, payload, extra=None):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _reject_method(self):
        self._send_json(405, {"result": "method not allowed"}, {"Allow": "POST"})

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reject_method

    # ---- POST ------------------------------------------------------------
    def do_POST(self):
        try:
            self._handle_post()
        except Exception:  # never leak a stack trace to the client
            LOG.exception("unhandled receiver error")
            try:
                self._send_json(500, {"result": "internal error"})
            except Exception:
                pass

    @staticmethod
    def _target_commit(payload, branch):
        """Return sha / None (no branch push) / False (malformed main target)."""
        push = payload.get("push") if isinstance(payload, dict) else None
        changes = push.get("changes") if isinstance(push, dict) else None
        if not isinstance(changes, list):
            return None
        target = None
        for change in changes:
            if not isinstance(change, dict):
                continue
            new = change.get("new")
            if not isinstance(new, dict):
                continue  # deletion (new=null) or malformed
            if new.get("type", "branch") != "branch":
                continue
            if new.get("name") != branch:
                continue
            if new.get("closed"):
                continue
            raw_target = new.get("target")
            if not isinstance(raw_target, dict):
                return False  # non-deleted branch push without a target object
            target = raw_target.get("hash")
            if not isinstance(target, str):
                return False
        if target is None:
            return None
        return target if SHA_RE.match(target) else False

    def _discard_body(self, length):
        """Drain an oversized request body (bounded) before answering.

        Closing the connection with unread client data queued can reset it
        (RST) and destroy the 413 response before the client reads it, so the
        body is read first -- bounded so a lying or hostile client cannot
        stall or exhaust us.
        """
        remaining = min(length, 8 * 1024 * 1024)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _handle_post(self):
        match = ENDPOINT_RE.match(self.path.split("?", 1)[0])
        if not match:
            self._send_json(404, {"result": "not found"})
            return
        site = match.group(1)
        cfg = self.app.sites.get(site)
        if cfg is None:
            self._send_json(404, {"result": "unknown site"})
            return
        if os.path.exists(cfg.kill_switch):
            LOG.warning("site=%s kill switch active at %s", site, cfg.kill_switch)
            self._send_json(503, {"result": "disabled"})
            return
        length_header = self.headers.get("Content-Length")
        if length_header is None or not length_header.isdigit():
            self._send_json(411, {"result": "length required"})
            return
        length = int(length_header)
        if length > MAX_BODY_BYTES:
            self._discard_body(length)
            self._send_json(413, {"result": "payload too large"})
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_json(400, {"result": "short body"})
            return
        # HMAC before JSON parsing -- exact design order.
        if cfg.secret is None:
            LOG.error("site=%s no webhook secret loaded", site)
            self._send_json(500, {"result": "receiver misconfigured"})
            return
        if not verify_signature(cfg.secret, body, self.headers.get("X-Hub-Signature")):
            LOG.warning("site=%s rejected delivery: bad signature", site)
            self._send_json(401, {"result": "bad signature"})
            return
        if self.headers.get("X-Event-Key") != REQUIRED_EVENT:
            LOG.info(
                "site=%s ignored delivery: event=%s",
                site,
                self.headers.get("X-Event-Key"),
            )
            self._send_json(400, {"result": "unexpected event"})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            LOG.info("site=%s rejected delivery: malformed JSON", site)
            self._send_json(400, {"result": "malformed json"})
            return
        repository = payload.get("repository") if isinstance(payload, dict) else None
        repo = repository.get("full_name", "") if isinstance(repository, dict) else ""
        if repo != cfg.repo_full_name:
            LOG.warning("site=%s rejected delivery: repository=%s", site, repo)
            self._send_json(403, {"result": "unexpected repository"})
            return
        commit = self._target_commit(payload, cfg.branch)
        if commit is False:
            LOG.info("site=%s rejected delivery: invalid commit hash", site)
            self._send_json(400, {"result": "invalid commit hash"})
            return
        if commit is None:
            LOG.info("site=%s ignored delivery: no non-deleted %s push", site, cfg.branch)
            self._send_json(202, {"result": "ignored"})
            return
        request_uuid = self.headers.get("X-Request-UUID")
        if not valid_run_id(request_uuid):
            LOG.warning(
                "site=%s rejected delivery: missing/malformed X-Request-UUID", site
            )
            self._send_json(400, {"result": "invalid request uuid"})
            return
        result = self.app.store.enqueue(site, request_uuid, commit, request_uuid)
        LOG.info(
            "site=%s commit=%s delivery=%s result=%s",
            site,
            commit[:12],
            request_uuid,
            result,
        )
        self._send_json(202, {"result": result, "site": site, "commit": commit})


class ReceiverApp:
    def __init__(self, config_dir, state_dir, bind="127.0.0.1", port=9087):
        self.config_dir = Path(config_dir)
        self.state_dir = Path(state_dir)
        self.bind = bind
        self.port = port
        self.sites = {}
        self._load_sites()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, STATE_DIR_MODE)
        except OSError as err:
            LOG.warning(
                "cannot enforce %o on %s: %s", STATE_DIR_MODE, self.state_dir, err
            )
        self.store = QueueStore(self.state_dir / "queue.db")
        self.worker = DeployWorker(self)
        self.server = None
        self._shutdown = threading.Event()
        self._supervisor = None

    def _load_sites(self):
        if not self.config_dir.is_dir():
            raise SystemExit("config dir not found: %s" % self.config_dir)
        for path in sorted(self.config_dir.glob("*.json")):
            raw = json.loads(path.read_text())
            cfg = SiteConfig(raw)
            try:
                cfg.load_secret()
            except OSError as err:
                LOG.error(
                    "site=%s cannot read webhook secret %s: %s",
                    cfg.site,
                    cfg.secret_file,
                    err,
                )
            self.sites[cfg.site] = cfg
        if not self.sites:
            raise SystemExit("no site configs in %s" % self.config_dir)

    def start(self):
        recovered = self.store.recover_running()
        if recovered:
            LOG.warning("recovered %d interrupted running job(s)", recovered)
        self.server = ThreadingHTTPServer((self.bind, self.port), _Handler)
        self.server.app = self
        self.worker.start()
        thread = threading.Thread(
            target=self.server.serve_forever, name="receiver-http", daemon=True
        )
        thread.start()
        self._http_thread = thread
        self._supervisor = threading.Thread(
            target=self._supervise_worker, name="worker-supervisor", daemon=True
        )
        self._supervisor.start()
        return self.server.server_address

    def supervise_worker_once(self) -> bool:
        """Restart a dead worker thread; True when a restart happened."""
        if self.worker.is_alive() or self.worker.is_stopping():
            return False
        LOG.error(
            "worker thread is not alive (errors=%d); restarting it",
            self.worker.errors,
        )
        self.worker = DeployWorker(self)
        self.worker.start()
        return True

    def _supervise_worker(self):
        while not self._shutdown.wait(WORKER_SUPERVISE_SECONDS):
            self.supervise_worker_once()

    def stop(self):
        self._shutdown.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.worker.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9087)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    app = ReceiverApp(args.config_dir, args.state_dir, args.bind, args.port)
    host, port = app.start()
    LOG.info(
        "listening on %s:%d sites=%s",
        host,
        port,
        ",".join(sorted(app.sites)),
    )
    stop_event = threading.Event()

    def _signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    stop_event.wait()
    LOG.info("shutting down")
    app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
