"""Run `[installs.<name>]` declarations: probe whether each is present, install if not.

Sits between `config` and `installer` in the dependency chain. Knows nothing
about the registry or ordering — `installer` decides *whether* an install runs
(dependencies, exclusions); this module only knows *how*.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from spg.config import Install

# How long `spg status` lets one install's probe run. Install/sync wait as long
# as a probe takes, since they are about to run an installer anyway.
STATUS_CHECK_TIMEOUT = 10.0

# Result of probing an install.
#   present — the probe says it is installed
#   absent  — the probe says it is not
#   timeout — the probe did not finish within the allowed time
#   error   — the probe could not be run at all (e.g. no /bin/sh)
ProbeState = Literal["present", "absent", "timeout", "error"]

# Result of ensuring an install.
#   present   — already installed; nothing ran
#   installed — `run` ran and the probe now passes
#   failed    — `run` failed, or ran but the probe still fails
EnsureStatus = Literal["present", "installed", "failed"]


@dataclass(frozen=True)
class EnsureOutcome:
    name: str
    status: EnsureStatus
    detail: str = ""


def probe(install: Install, root: Path, *, timeout: float | None = None) -> ProbeState:
    """Whether `install` is already present, without changing anything."""
    creates = install.creates_path
    if creates is not None:
        return "present" if creates.exists() or creates.is_symlink() else "absent"
    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", install.check],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "error"
    return "present" if completed.returncode == 0 else "absent"


def ensure(install: Install, root: Path) -> EnsureOutcome:
    """Install `install` unless its probe says it is already present.

    `run` executes with the project root as CWD and the user's terminal attached
    (installers print progress and sometimes prompt). After a successful run the
    probe is repeated: an installer that exits 0 but leaves the tool where the
    probe cannot see it (typically: not on PATH) is reported as a failure, since
    anything that requires it would otherwise go ahead and break.
    """
    before = probe(install, root)
    if before == "present":
        return EnsureOutcome(install.name, "present")
    try:
        completed = subprocess.run(["/bin/sh", "-c", install.run], cwd=root, check=False)
    except OSError as exc:
        return EnsureOutcome(install.name, "failed", f"could not run: {exc.strerror or exc}")
    if completed.returncode != 0:
        return EnsureOutcome(
            install.name, "failed", f"run exited with status {completed.returncode}"
        )
    after = probe(install, root)
    if after == "present":
        return EnsureOutcome(install.name, "installed")
    probe_kind = f"'creates' path {install.creates}" if install.creates else "'check'"
    return EnsureOutcome(
        install.name,
        "failed",
        f"run succeeded but {probe_kind} still reports it missing "
        "(is it installed somewhere not on PATH?)",
    )


__all__ = [
    "STATUS_CHECK_TIMEOUT",
    "EnsureOutcome",
    "EnsureStatus",
    "ProbeState",
    "ensure",
    "probe",
]
