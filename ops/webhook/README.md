# ops/webhook — 7YRDS production deploy webhook pipeline

Bitbucket `repo:push` -> `https://7yards-bau.uhp-software.com/__deploy/hooks/bitbucket/bau`
-> exact-commit build on production -> atomic release swap.

Binding design: `/opt/data/wf/t_7ccb4522/design.md` (TRIAGE-7).  Builds run
ON PRODUCTION as the locked, unprivileged `yrds-build` user (no token, no
key, private per-run HOME/npm cache, 15-minute budget, cgroup caps).  The
old `/opt/deploy/deploy.sh` stays in place untouched; application rollback
remains `/opt/deploy/rollback.sh`.

## Repository layout

    bin/receiver.py            stdlib HTTP receiver, queue/dedupe, one worker
    bin/deploy-site            root-only fetch/build/validate/activate pipeline
    bin/install                idempotent installer (never registers Bitbucket objects)
    bin/healthcheck            non-secret local state report
    systemd/7yrds-webhook.service   hardened unit, 640M/768M/100%/256/Nice10
    nginx/bau-webhook.conf     exact-match proxy snippet (1 MiB body cap)
    sites/bau.json             non-secret per-site mapping
    tests/                     hermetic tests (temp roots + command stubs)

## Installed paths

    /opt/7yrds-deploy/                     root-owned code
    /etc/7yrds-deploy/sites.d/bau.json     site map
    /etc/7yrds-deploy/secrets/bau.webhook  0400 root, never printed
    /etc/7yrds-deploy/ssh/bau_ed25519      private 0400 root; known_hosts pinned
    /var/lib/7yrds-deploy/                 repos/, work/, queue.db (root-only)
    /var/log/7yrds-deploy/bau/<run-id>.log per-run logs (+ journald)
    /run/lock/7yrds-deploy-bau.lock        per-site flock
    127.0.0.1:9087                         receiver bind

## Install (production, root, via the VM hop)

    ops/webhook/bin/install bau
    # then, deliberate manual steps:
    #  - include /etc/nginx/snippets/7yrds-webhook-bau.conf inside the bau TLS
    #    server block exactly once; nginx -t && nginx -s reload
    #  - systemctl enable --now 7yrds-webhook
    #  - Bitbucket: register the deploy key + webhook (separate approved step)

The installer is idempotent (content-compared), backs up files it replaces
under `/var/backups/7yrds-deploy/<ts>/`, generates the webhook secret and
deploy key only when absent, and never prints a secret value.  Reruns
re-assert owner/mode on existing secrets, keys, directories and installed
files (drift is repaired, not preserved).  `known_hosts` is pinned to the
embedded Bitbucket ED25519 key: an existing file is kept only when every
entry matches Atlassian's published fingerprint, otherwise it is replaced,
and the installer fails if the pinned line itself cannot be verified (no
network needed).  It does not create or register anything at Bitbucket and
never touches `/opt/deploy/`.

## Security model

- HMAC-SHA256 (`X-Hub-Signature`) is verified over the raw body **before**
  JSON parsing; missing/malformed/wrong -> 401.
- Only `repo:push` (else 400), only `uhpsoftware/7yrds-bau` (else 403),
  only a non-deleted `main` branch push with a full 40-hex commit queues.
- Durable dedupe key: site + `X-Request-UUID` + commit, in root-only
  SQLite; retries of queued/running/completed deliveries return 202
  `duplicate` and start no second build.  Missing/malformed
  `X-Request-UUID` (including `.`/`..`) is rejected with 400 before any
  enqueue, and a header value never selects a work/log path.
- Queue state is root-only: `/var/lib/7yrds-deploy` is 0711 (traverse-only
  for the unprivileged build user -- never listable or writable by it) and
  `queue.db` plus its WAL/SHM sidecars are 0600 (service `UMask=0077`,
  re-asserted by the receiver on every connection).
- One serial worker holds `/run/lock/7yrds-deploy-<site>.lock` for the whole
  deploy; interrupted `running` rows are recovered to `queued` at startup.
  Worker/store/deploy-launch exceptions are contained and recorded, a job
  can never be left `running` by them, and a supervisor thread restarts the
  worker if it ever dies (errors are logged at ERROR level).
- Logs contain ids/commits/status only — never bodies, signatures,
  secrets, tokens or key material.
- The build user has no key and no secret access; releases are root-owned.
- Kill switch: `touch /etc/7yrds-deploy/DISABLED` -> receiver answers 503
  without enqueuing.  `systemctl stop 7yrds-webhook` is the hard stop.

## Deploy pipeline (deploy-site)

1. Preflight: >= 2 GiB free on `/`; measures disk/MemAvailable/swap.
2. No-op check: full SHA already live in `RELEASE-METADATA.txt` -> exit 0,
   nothing built/swapped/pruned (safe replay).  A stale/missing compatibility
   record is repaired first, so a retry of a failed record refresh converges
   instead of silently skipping.
3. Fetch `refs/heads/main` with the root-only key (`IdentitiesOnly=yes`,
   pinned known_hosts, strict checking) into the bare repo.
4. Validate the payload SHA is a commit on the fetched `main`; extract
   that exact commit with `git archive` (no checkout hooks, no token).
5. Build as `yrds-build`: `npm ci --no-audit --no-fund`, `npm run build`
   with `CI=1`, `NODE_OPTIONS=--max-old-space-size=512`, 15-minute budget.
6. Validate non-empty `dist/index.html`; create the release and its
   `RELEASE-METADATA.txt` (commit, branch, source, request UUID/run id,
   build/activation UTC timestamps).
7. `nginx -t` -> atomic `current.tmp` + `mv -T` swap -> `nginx -t` ->
   local-TLS smoke request forced to 127.0.0.1: status 200 AND response
   body SHA256 equal to the release `index.html`.
8. Any post-swap failure: previous symlink restored atomically; no prune.
   Reload failure: same restore, then failure exit.
9. Success: refresh `/var/www/bau/RELEASE-METADATA.txt` (retried; a
   persistent failure exits 6 while the verified release stays live and the
   next attempt repairs the record), prune to newest 3 timestamp-named
   releases (never `current`, never non-release entries), delete workspaces
   older than 72 h.  Cleanup failures are logged, never fail a verified
   deploy.

Exit codes: 0 ok/noop | 2 usage/config/refused | 3 fetch/commit validation |
4 build | 5 pre-activation (disk floor, pre-swap `nginx -t`) |
6 post-swap validation failed (previous release restored) or the verified
release stays live with a failed compatibility-record refresh | 7 reload
failed (previous release restored).

## Manual paths (root only)

    /opt/7yrds-deploy/bin/deploy-site --site bau --commit <40-sha> --source manual
    /opt/7yrds-deploy/bin/deploy-site --site bau --commit <40-sha> --source manual-test --build-only
    /opt/7yrds-deploy/bin/deploy-site --site bau --commit <40-sha> --source manual-test --simulate-build-failure

- break glass: same code path as the webhook, `--source manual` (no test modes).
- `--source manual-test` is the only accepted source for the test modes:
  `--build-only` fetches/extracts/builds/measures, creates no release and
  never touches `current`; `--simulate-build-failure` bypasses the live-commit
  no-op, fails inside the disposable workspace and must leave `current`
  unchanged.  Both modes are refused for `webhook` and for break-glass
  `manual`, and `manual-test` alone (without a test mode) is refused too.

## Add a site

1. Add `sites/<slug>.json` and `nginx/<slug>-webhook.conf` (copy `bau`).
2. Run `ops/webhook/bin/install <slug>` (it also installs the snippet).
3. Include the snippet in the site's TLS server block; `nginx -t`, reload.
4. Create the Bitbucket webhook (`repo:push`) with the freshly generated
   secret — out-of-band approved step; the installer never does this.
5. `systemctl restart 7yrds-webhook` to load the new site map.

## Tests (hermetic; run as root on the build VM)

    cd ~/repo/7yrds-bau
    python3 -m unittest discover -s ops/webhook/tests -p 'test_*.py' -v

They use temporary roots and command stubs only: no production access, no
network, no real git/npm/nginx/curl.  Coverage: Atlassian HMAC vector, bad
signature, non-POST, wrong event/repo/branch, main enqueue, duplicate
delivery, worker serialization, exact-commit validation, pre-activation
build failure, post-swap smoke rollback, prune guard and log redaction.
Regression coverage (review 2026-09-29): missing/malformed `X-Request-UUID`
and dot run-ids rejected, `.`/`..` never select work/log paths, malformed
signed nested payloads return controlled 4xx, durable enqueue before the
202 (and no waiting for work), queue.db + WAL/SHM creation modes, state and
work chain traversal modes for the unprivileged build user (0711, never
writable), worker
containment (missing deploy executable, SQLite errors, watchdog restart),
`manual-test` source gating, compatibility-record failure/repair paths, and
installer drift reruns (secret/key/dir modes, pinned known_hosts).
