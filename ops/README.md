# 7YRDS ops — production webhook pipeline + retained release tooling

This directory documents the operations layer of the 7YRDS production host
(ssh alias `7yrds-linode`, hostname `7yrds-prod`; Debian 13, nginx 1.26.3).
One box hosts every 7YRDS website — each additional site is a **config
add-on, not a rebuild**.

Two layers exist side by side:

1. **Webhook pipeline (primary)** — `ops/webhook/` (this repository).
   A Bitbucket `repo:push` to
   `https://<site>/__deploy/hooks/bitbucket/<site>` makes production fetch
   the pushed commit with a root-only read-only deploy key, build it there
   as the locked, unprivileged `yrds-build` user, and activate it with an
   atomic symlink swap proven by a local-TLS body-hash smoke check.
   Architecture, install steps, security model and the add-a-site recipe:
   `ops/webhook/README.md`.
2. **Legacy release tooling (retained, untouched)** — `/opt/deploy/` on the
   deploy host: `deploy.sh` (VM-built rsync deploy) and the
   `rollback.sh` rollback, plus its own README. Both stay **byte-identical**
   (`deploy.sh` sha256 `4685db25…`, `rollback.sh` sha256 `15b9f445…`); the
   webhook pipeline does not modify, wrap or replace them, and application
   rollback remains `rollback.sh` for every path.

Provenance: this document is reconciled against the installed
`/opt/deploy` tooling and the end-to-end proof run of 2026-09-25 (board
`7yrds-bau`, task `t_cd402451`), and against the TRIAGE-7 webhook design
(`/opt/data/wf/t_7ccb4522/design.md`).

## 1. Release layout — what nginx serves, and why the swap is atomic

    /var/www/<site>/releases/<UTC-timestamp>/   one immutable release (never served directly)
    /var/www/<site>/current -> releases/<ts>    the symlink nginx serves as its root

- `<site>` = slug `^[a-z0-9-]+$` (lowercase, digits, hyphens); identical to
  the vhost file name `/etc/nginx/sites-available/<site>.conf`.
- `<UTC-timestamp>` = `date -u +%Y%m%dT%H%M%SZ` (e.g. `20260925T170811Z`;
  regex `^[0-9]{8}T[0-9]{6}Z$`), so releases sort chronologically by name.
- Every site vhost sets `root /var/www/<site>/current;` — nginx always serves
  the symlink, never a release directory.
- **Atomicity:** activation replaces the `current` symlink with a single
  rename — `rm -f current.tmp; ln -sfn <release> current.tmp;
  mv -T current.tmp current`. rename(2) over the symlink object is atomic:
  every request sees either the old or the new release; `current` is never
  momentarily missing, and a release is fully populated before it is named.
  Both the legacy `deploy.sh` and the webhook `deploy-site` use this exact
  mechanism. Never `rm`+`ln` the symlink by hand — that reintroduces the
  missing-link window.
- Release content is normalized to root-owned files 0644 / dirs 0755.
- Every release carries `RELEASE-METADATA.txt` (full commit, branch, trigger
  source `bitbucket-webhook`/`manual`, request UUID/run id, build and
  activation UTC timestamps). The webhook pipeline also refreshes the
  compatibility record `/var/www/<site>/RELEASE-METADATA.txt` after each
  successful activation (retried; if it cannot be written the verified
  release stays live, the run reports failure, and the next attempt repairs
  the record instead of no-oping over stale metadata). The live-commit
  no-op check reads the release metadata, so it must always name the exact
  commit that was built.
- Keep the newest 3 timestamp-named releases; never delete the target of
  `current`, never touch non-timestamp entries (both prunes enforce this).
- `/var/www/html` is the Debian stock nginx web root (served by the stock
  default vhost as fallback for unmatched hostnames) — not part of the site
  convention.

## 2. Shared nginx snippets

All in `/etc/nginx/snippets/`; every site vhost includes them:

- `security-headers.conf` — include at `server{}` level, **and again inside
  any `location{}` block that declares its own `add_header`** (nginx stops
  inheriting `add_header` into such blocks). Sets `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy`. HSTS is added only
  after HTTPS is verified live; CSP is left to per-site tuning.
- `gzip.conf` — include at `http{}` or `server{}` level. gzip for text
  content; `text/html` is compressed by nginx unconditionally and must NOT
  be listed in `gzip_types` (duplicate-MIME warning).
- `static-cache.conf` — include at `server{}` level only (it contains
  `location{}` blocks). Year-long immutable `Cache-Control` for `/_astro/`
  and `/images/`; each location re-includes the security headers on purpose.
- `7yrds-webhook-<site>.conf` — the webhook proxy snippet
  (`ops/webhook/nginx/`). Exact-match `location = /__deploy/hooks/bitbucket/
  <site>` proxying to `http://127.0.0.1:9087` with `client_max_body_size
  1m` and short connect/read timeouts. Include it **exactly once** inside
  the site's TLS server block; it changes nothing else (no TLS, redirects,
  headers, caching, other vhosts).
- `_template.conf` in `/etc/nginx/sites-available/` — the vhost template
  (its header carries the onboarding checklist). New vhosts are copied from
  it; never symlink `_template.conf` itself into `sites-enabled/`.

## 3. Deploy paths

### 3a. Primary — webhook pipeline (production builds the pushed commit)

Bitbucket `repo:push` (branch `main` only) -> nginx exact path
`/__deploy/hooks/bitbucket/<site>` -> receiver on `127.0.0.1:9087`
(HMAC-verified before JSON, durable dedupe, one serial worker) ->
`/opt/7yrds-deploy/bin/deploy-site`: exact-commit fetch with the root-only
read-only deploy key, unprivileged `npm ci && npm run build` as
`yrds-build`, release creation outside `/var/www`, `nginx -t`, atomic swap,
post-swap `nginx -t` + local-TLS body-hash smoke check, automatic previous-
symlink restore on any post-swap failure, keep-3 prune, 72 h stale-work
cleanup.  Full contract, exit codes, break-glass and test modes:
`ops/webhook/README.md`.

### 3b. Legacy — `deploy.sh` (retained byte-identical, VM-built rsync)

Still usable as recorded below; it is no longer the steady-state path (the
pipeline builds on production from the pushed commit instead).

    /opt/deploy/deploy.sh [options] <site-slug> <local-build-dir>

    -e, --exclude PATTERN   rsync exclude, repeatable
    -c, --check-cmd CMD     custom release validation (replaces non-empty index.html check)
    -k, --keep N            releases to keep, newest first (default: 3)
    -H, --host HOST         deploy host ssh alias (default: 7yrds-linode)
    -n, --dry-run           print the plan + current remote state; no changes
    -h, --help              usage

One run: local preflight -> one multiplexed ssh connection -> create
`releases/<UTC-ts>` -> rsync the build dir -> validate -> `nginx -t` ->
atomic swap -> `nginx -t` (+ restore + no reload on failure) -> reload ->
prune keep-N. Exit codes: `0` success | `1` usage/local error (nothing
changed) | `2` failed before activation | `3` post-swap `nginx -t` failed
(previous release restored, no reload) | `4` reload failed (release IS
active) | `5` prune failed (release IS active).

## 4. Rollback — `rollback.sh` (unchanged, production-only)

Runs on the deploy host as root; invoke it over the VM hop:

    ssh -F /opt/data/home/.ssh/config uhp-7yrds-bau-local \
      'ssh 7yrds-linode "/opt/deploy/rollback.sh <site-slug> [<release-timestamp>]"'

    /opt/deploy/rollback.sh [--list] <site-slug> [<release-timestamp>]

One run: preflight (root; at least 2 releases; named target must exist and
not be current) -> `nginx -t` -> atomic swap of `current` -> `nginx -t`
(post-swap failure restores the previous state, no reload) -> reload.
Exit codes: `0` success | `1` preflight (nothing changed) | `2` no rollback
possible | `3` `nginx -t` failed | `4` reload failed (rollback IS active).

`rollback.sh` only moves the `current` symlink; it never creates, modifies
or deletes release dirs. Only the newest 3 releases are kept — check
`--list` first.

## 5. Onboarding site #2..N — copy-paste checklist

**A new site is a config add-on, not a rebuild**: one vhost file + one web
root + one `sites.d` entry. No package changes, no `nginx.conf` edits, no
restarts (graceful reloads only).

1. Pick the slug `^[a-z0-9-]+$` — it names the vhost, `/var/www/<site>`,
   the lock file and the log dir.
2. On the deploy host (via the VM hop, as root), create the vhost from the
   template:

       cp -p /etc/nginx/sites-available/_template.conf \
             /etc/nginx/sites-available/<site>.conf

   Replace **every** `<site>` placeholder and set `server_name` (e.g.
   `7yards-bau.uhp-software.com` — the "7yards" spelling is intentional).
3. Enable, test, reload:

       ln -s /etc/nginx/sites-available/<site>.conf \
             /etc/nginx/sites-enabled/<site>.conf
       nginx -t && systemctl reload nginx

4. Webhook pipeline (from the reviewed repository tree on the build VM,
   copied to production as root): add `ops/webhook/sites/<site>.json` and
   `ops/webhook/nginx/<site>-webhook.conf`, copy `ops/webhook/**` to the
   production host as reviewed, then run `ops/webhook/bin/install <site>`,
   include the snippet in the TLS server block, `nginx -t`, reload,
   `systemctl restart 7yrds-webhook`, and only then register the Bitbucket
   webhook + read-only deploy key (separate approved step; the installer
   never touches Bitbucket).
5. Verify: `curl -sI https://<hostname>/` (200), and
   `/opt/deploy/rollback.sh --list <site>`.

Notes: DNS lives outside the box — point the A record at 139.162.175.22
there. TLS is added by certbot (never hand-roll it in the vhost); HSTS only
after HTTPS is verified live. The stock default vhost stays the fallback.
Decommissioning is manual and approved: remove the `sites-enabled` symlink,
reload nginx, remove the `sites.d` entry, then delete `/var/www/<site>`
(guard the `rm`).

## 6. Constraints and ground rules

- **Builds**: the webhook pipeline builds on production as the locked
  `yrds-build` user (no token/key, capped, logged). `deploy.sh` remains the
  legacy path where the build VM ships a prebuilt directory.
- **All access goes through the VM hop** (`ssh uhp-7yrds-bau-local 'ssh
  7yrds-linode "..."'`). The `deploy` user's sudo needs a password (not
  recorded) — use the root path for privileged remote commands.
- **No secrets** in scripts, docs or build directories. The webhook secret
  and deploy key live only as 0400 root files under `/etc/7yrds-deploy/`;
  neither is ever printed or committed. The Bitbucket read token stays on
  the VM. The queue/dedupe state (`/var/lib/7yrds-deploy`) is root-only
  (0711 traverse-only dir, 0600 `queue.db` + WAL/SHM, service `UMask=0077`).
- **Pace ssh:** the deploy host rate-limits port 22 — space out ad-hoc
  checks; scripts multiplex connections.
- **Keep the newest 3 releases; never delete the target of `current`.**
- **Kill switch:** `touch /etc/7yrds-deploy/DISABLED` pauses the receiver
  (503, nothing enqueued); `systemctl stop 7yrds-webhook` is the hard stop.
- After a graceful reload, the first request may still be answered by an
  outgoing worker with the pre-reload config. Steady state is correct.

## Proven

End-to-end proof, 2026-09-25 (board `7yrds-bau`, task `t_cd402451`): two
throwaway sites onboarded from the template and deployed with `deploy.sh`;
a swap under a live 300-request loop flipped v1 to v2 with 0 errors;
rollback restored v1 with a matching sha256; prune kept exactly 3 releases;
`nginx -t` ran before and after every swap; both sites were then removed.
Site `bau` (release #1 commit `0373a90`, release #2 commit `c6f38921`)
follows this layout live. The webhook pipeline (`ops/webhook/`) is
specified by TRIAGE-7; its hermetic test suite lives in
`ops/webhook/tests/`.
