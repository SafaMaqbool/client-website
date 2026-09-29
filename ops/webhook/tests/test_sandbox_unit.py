#!/usr/bin/env python3
"""Sandbox regression test for ops/webhook/systemd/7yrds-webhook.service.

Defect (2026-09-29, bisected on production): the reviewed unit carried
explicit ``User=root`` and ``Group=root`` lines.  Interacting with the unit's
hardening block, systemd launched the receiver with CAP_SETUID removed from
the permitted/effective sets (CapEff=CapPrm=0x46b against CapBnd=0x4eb), so
the root-only deploy-site could not drop to the unprivileged build user:
``runuser: cannot set user id: Operation not permitted``, deploy-site exit 4,
no release built.  The fix removes ONLY those two lines: the service then
runs as root by default (its design intent -- it needs the capability set to
drop privileges per build).  ``AmbientCapabilities`` was explicitly rejected
as a fix: ambient capabilities survive execve and would be inherited by the
unprivileged build child, letting a compromised build script call setuid(0).

This suite replays the unit's exact [Service] property set in a transient
systemd unit (sandbox and capability directives verbatim; the probe payload
replaces ExecStart) and asserts:

  1. under that property set a uid drop to the unprivileged user succeeds;
  2. the unprivileged build child has EMPTY CapInh/CapPrm/CapEff/CapAmb
     (leak check, read from /proc/<pid>/status), and the root process still
     carries the full reviewed bounding set (no CAP_SETUID strip);
  3. no AmbientCapabilities directive exists anywhere in the unit set;
  4. the unit carries no explicit User=/Group= and the reviewed capability
     bounding set is not widened.

It FAILS on the pre-patch unit (runuser EPERM, exit != 0) and PASSES on the
patched one.  Run as root on the build VM: the replay needs systemd and
util-linux runuser.  No production access, no network, nothing installed.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
UNIT_DIR = TESTS_DIR.parent / "systemd"
UNIT_FILE = UNIT_DIR / "7yrds-webhook.service"

# The reviewed capability bounding set (may not be widened; CAP_SETUID is the
# capability the root receiver needs to drop to the build user).
REVIEWED_CAPABILITY_BOUNDING_SET = (
    "CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_KILL CAP_SETGID CAP_SETUID "
    "CAP_NET_BIND_SERVICE"
)

# Process-lifecycle directives are not replayed: the probe payload replaces
# ExecStart, and a restart policy would distort the replay's exit semantics.
# Every sandbox, credential and capability directive is replayed verbatim.
REPLAY_EXCLUDED = {
    "Type",
    "ExecStart",
    "Restart",
    "RestartSec",
    "KillSignal",
    "TimeoutStopSec",
}

# Runs inside the transient unit as its root process.  argv:
#   [1] build user  [2] absolute runuser path  [3] absolute child python
# It drops to the build user via runuser and prints the child's uid and
# /proc/self/status capability lines (children read their own status file),
# then the parent's capability lines, and exits 1 when the drop fails.
SANDBOX_PROBE = r'''
import subprocess, sys
user, runuser, child_python = sys.argv[1], sys.argv[2], sys.argv[3]
child_src = (
    "import os\n"
    "print('CHILD_UID=%d' % os.getuid())\n"
    "for line in open('/proc/self/status'):\n"
    "    if line.split(':')[0] in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):\n"
    "        print('CHILD_' + line.rstrip())\n"
)
result = subprocess.run(
    [runuser, "-u", user, "--", child_python, "-B", "-c", child_src],
    capture_output=True, text=True,
)
print("RUNUSER_RC=%d" % result.returncode)
sys.stdout.write(result.stdout)
if result.stderr.strip():
    print("RUNUSER_ERR=%s" % result.stderr.strip().splitlines()[-1])
for line in open('/proc/self/status'):
    if line.split(':')[0] in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
        print('PARENT_' + line.rstrip())
sys.exit(1 if result.returncode != 0 else 0)
'''


def parse_service_directives(path):
    """Return the [Service] section as [(key, value)] in file order."""
    directives = []
    section = None
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line
            continue
        if section == "[Service]" and "=" in line:
            key, value = line.split("=", 1)
            directives.append((key.strip(), value.strip()))
    return directives


def replay_property(key, value):
    """Normalize one directive for replay on a host where the production paths
    may be absent.  Only start-conditions are touched, using systemd's own
    ``-`` ignore-if-missing prefix; credential and capability semantics stay
    byte-exact."""
    if key == "WorkingDirectory" and not value.startswith("-") and not os.path.exists(value):
        return "-" + value
    if key == "ReadWritePaths":
        return " ".join(
            p if p.startswith("-") or os.path.exists(p) else "-" + p
            for p in value.split()
        )
    return value


def pick_build_user():
    for candidate in ("yrds-build", "nobody", "daemon", "games"):
        try:
            record = pwd.getpwnam(candidate)
        except KeyError:
            continue
        if record.pw_uid != 0:
            return candidate
    return None


def parse_probe_output(output):
    values = {}
    for line in output.splitlines():
        if line.startswith("RUNUSER_RC="):
            values["RUNUSER_RC"] = int(line.split("=", 1)[1])
        elif line.startswith("CHILD_UID="):
            values["CHILD_UID"] = int(line.split("=", 1)[1])
        elif line.startswith(("CHILD_Cap", "PARENT_Cap")):
            key, value = line.split(":", 1)
            values[key] = value.strip()
    return values


class SandboxReplayTests(unittest.TestCase):
    """Kernel-enforced replay of the unit sandbox: uid drop + leak check."""

    maxDiff = None

    def setUp(self):
        if os.geteuid() != 0:
            raise unittest.SkipTest("sandbox replay needs root for systemd-run")
        if shutil.which("systemd-run") is None or not os.path.exists("/run/systemd/system"):
            raise unittest.SkipTest("sandbox replay needs a running systemd")
        self.build_user = pick_build_user()
        if self.build_user is None:
            raise unittest.SkipTest("no unprivileged system user available")

    def test_uid_drop_succeeds_and_child_caps_are_empty(self):
        """(i) uid drop under the unit's exact property set succeeds and
        (ii) the unprivileged build child carries no capabilities at all.
        Fails on the pre-patch unit: explicit User=/Group= plus the hardening
        block made systemd strip CAP_SETUID from the root process, so runuser
        exits 1 with 'cannot set user id: Operation not permitted'."""
        runuser = shutil.which("runuser") or "/usr/sbin/runuser"
        child_python = shutil.which("python3") or sys.executable
        unit_name = "yrds-sandbox-test-" + uuid.uuid4().hex[:12]
        command = [
            "systemd-run",
            "--wait",
            "--pipe",
            "--collect",
            "--quiet",
            "--unit=" + unit_name,
        ]
        for key, value in parse_service_directives(UNIT_FILE):
            if key in REPLAY_EXCLUDED:
                continue
            command.append("--property=%s=%s" % (key, replay_property(key, value)))
        command += [
            sys.executable,
            "-B",
            "-c",
            SANDBOX_PROBE,
            self.build_user,
            runuser,
            child_python,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        output = "SYSTEMD_RUN_RC=%d\n%s%s" % (result.returncode, result.stdout, result.stderr)
        values = parse_probe_output(result.stdout)
        self.assertEqual(
            result.returncode,
            0,
            "uid drop or sandbox replay failed -- the unit must not carry an "
            "explicit User=/Group= next to the hardening block (CAP_SETUID is "
            "stripped from the root process):\n" + output,
        )
        self.assertEqual(values.get("RUNUSER_RC"), 0, output)
        record = pwd.getpwnam(self.build_user)
        self.assertEqual(values.get("CHILD_UID"), record.pw_uid, output)
        self.assertNotEqual(values.get("CHILD_UID"), 0, output)
        for cap in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
            self.assertEqual(
                values.get("CHILD_" + cap),
                "0000000000000000",
                "unprivileged build child must carry no %s "
                "(capability leak):\n%s" % (cap, output),
            )
        # Strip regression: the root process keeps the full reviewed bounding
        # set in permitted/effective (pre-patch CapEff=CapPrm=0x46b=0x4eb-0x80).
        self.assertEqual(values.get("PARENT_CapPrm"), values.get("PARENT_CapBnd"), output)
        self.assertEqual(values.get("PARENT_CapEff"), values.get("PARENT_CapBnd"), output)
        self.assertEqual(
            values.get("PARENT_CapBnd"),
            "%016x" % _caps_to_mask(REVIEWED_CAPABILITY_BOUNDING_SET),
            output,
        )


def _caps_to_mask(capability_string):
    """CAP_* names of the reviewed set as a hex capability mask."""
    values = {
        "CAP_CHOWN": 0,
        "CAP_DAC_OVERRIDE": 1,
        "CAP_FOWNER": 3,
        "CAP_KILL": 5,
        "CAP_SETGID": 6,
        "CAP_SETUID": 7,
        "CAP_NET_BIND_SERVICE": 10,
    }
    mask = 0
    for name in capability_string.split():
        mask |= 1 << values[name]
    return mask


class UnitSandboxStaticTests(unittest.TestCase):
    """Static guards on the reviewed unit text (no root/systemd needed)."""

    def test_no_ambient_capabilities_directive_anywhere_in_unit_set(self):
        """(iii) AmbientCapabilities must appear nowhere in the unit set:
        ambient capabilities survive execve and would be inherited by the
        unprivileged build child (setuid(0) from a compromised build)."""
        pattern = re.compile(r"^\s*AmbientCapabilities\s*=", re.IGNORECASE)
        offenders = []
        for path in sorted(UNIT_DIR.glob("*")):
            if not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.match(line):
                    offenders.append("%s:%d: %s" % (path.name, lineno, line.strip()))
        self.assertEqual(
            offenders, [],
            "AmbientCapabilities is rejected as a fix -- it would leak "
            "capabilities into the unprivileged build child",
        )

    def test_no_explicit_user_or_group_directive(self):
        """The unit must not carry an explicit User=/Group=: with the hardening
        block, systemd strips CAP_SETUID from the root process (the 2026-09-29
        production defect).  The service runs as root by default and needs the
        bounding set to drop privileges per build."""
        directives = dict(parse_service_directives(UNIT_FILE))
        offenders = [key for key in ("User", "Group") if key in directives]
        self.assertEqual(
            offenders, [],
            "explicit User=/Group= re-introduces the CAP_SETUID strip; if a "
            "future change needs one, re-prove the uid drop under the full "
            "property set first",
        )

    def test_capability_bounding_set_is_the_reviewed_set(self):
        directives = dict(parse_service_directives(UNIT_FILE))
        self.assertEqual(
            directives.get("CapabilityBoundingSet"),
            REVIEWED_CAPABILITY_BOUNDING_SET,
            "the capability bounding set must stay at the reviewed set "
            "(neither widened nor narrowed)",
        )


if __name__ == "__main__":
    unittest.main()
