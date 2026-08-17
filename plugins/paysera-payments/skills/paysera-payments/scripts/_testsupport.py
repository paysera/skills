"""Shared helpers for the script tests.

The scripts are named with hyphens (`create-payment.py`), so they cannot be imported
normally — they are loaded by path. Not named `test_*`, so no test runner collects it.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import os
import shutil
import stat
import tempfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent

# --- leak check -----------------------------------------------------------------------
# Every fixture here makes a temporary directory holding a ledger with test IBANs and
# amounts, and CI runs the whole suite twice. Rather than checking that each one is
# cleaned up, give the module its OWN temporary directory and require it to be empty at
# the end: no per-site bookkeeping, and a fixture added later is covered for free.
#
# `tempfile.tempdir` covers this process; the TMPDIR/TEMP/TMP variables cover the
# subprocesses the end-to-end tests spawn, which inherit the environment.
#
# HOME is redirected too, and is a different problem from the others. The temp box catches
# what the suite WRITES; HOME decides what the suite REACHES. Since 1.8.8 both scripts
# chmod ~/.config/paysera-payments/ to 0700 on every run that reads the token, so a test
# that inherited the real HOME reached outside the sandbox and changed the mode of the
# developer's own directory — silently, and invisibly to the temp-box check, which only
# ever looks inside the box. The redirect is here rather than in each fixture so that the
# next helper to read HOME is covered without anyone remembering.
#
# The script modules resolve their HOME-derived constants at IMPORT time, which is before
# any setUpModule runs — so redirecting the variable is not enough for in-process calls,
# and redirect_config_paths() re-points those constants as well.
_TEMPBOX = {}
_HOME_DERIVED = ("DEFAULT_TOKEN_FILE", "LEDGER_FILE")


def isolate_tempdir():
    """Use as `setUpModule`. Give this module a private, empty temporary directory."""
    # Made BEFORE tempfile.tempdir is redirected, so it lands in the real temporary
    # directory rather than inside the box the teardown requires to be empty.
    home = tempfile.mkdtemp(prefix="paysera-testhome-")
    box = tempfile.mkdtemp(prefix="paysera-tempbox-")
    _TEMPBOX["path"] = box
    _TEMPBOX["home"] = home
    _TEMPBOX["tempdir"] = tempfile.tempdir
    _TEMPBOX["env"] = {k: os.environ.get(k) for k in ("TMPDIR", "TEMP", "TMP", "HOME")}
    _TEMPBOX["constants"] = []
    tempfile.tempdir = box
    os.environ.update(TMPDIR=box, TEMP=box, TMP=box, HOME=home)


def redirect_config_paths(*modules):
    """Point each module's HOME-derived path constants inside the sandboxed HOME.

    Call from `setUpModule`, after isolate_tempdir(). Needed because the scripts run
    os.path.expanduser() at import time, so a later HOME redirect does not move a constant
    that was already resolved — and every in-process read_token() would still chmod the
    real ~/.config/paysera-payments.
    """
    home = _TEMPBOX.get("home")
    if home is None:
        raise AssertionError("isolate_tempdir() must run first — there is no sandbox HOME")
    config = os.path.join(home, ".config", "paysera-payments")
    for module in modules:
        for name in _HOME_DERIVED:
            if not hasattr(module, name):
                continue
            _TEMPBOX["constants"].append((module, name, getattr(module, name)))
            setattr(module, name, os.path.join(config, os.path.basename(getattr(module, name))))


def assert_tempdir_is_empty():
    """Use as `tearDownModule`. Fail if the module left anything in its directory."""
    # Raise rather than return: a silent no-op here is the exact failure this check exists
    # to catch — a disarmed check and a clean run are the same green otherwise.
    if "path" not in _TEMPBOX:
        raise AssertionError("setUpModule did not run — the leak check is disarmed")
    box = _TEMPBOX.pop("path")
    tempfile.tempdir = _TEMPBOX.pop("tempdir")
    for module, name, value in _TEMPBOX.pop("constants", []):
        setattr(module, name, value)
    for key, value in _TEMPBOX.pop("env").items():
        os.environ.pop(key, None) if value is None else os.environ.update({key: value})
    shutil.rmtree(_TEMPBOX.pop("home"), ignore_errors=True)
    left = sorted(os.listdir(box))
    shutil.rmtree(box, ignore_errors=True)
    if left:
        raise AssertionError(
            f"{len(left)} temporary item(s) left behind by this module — every mkdtemp() "
            f"needs a matching cleanup: {left[:5]}"
        )


def load(script_name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / script_name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def frozen_clock(module, when):
    """Pin `module`'s view of the current time to `when` (an aware datetime).

    Both `datetime.datetime.now()` and `time.time()` are pinned, because the scheduling
    code cross-checks one against the other.

    NOTE: the scripts call `datetime.datetime.now()` through the imported module, so this
    patches the shared `datetime`/`time` modules for the whole process, not a script-local
    alias. It is restored on exit, but it is NOT safe to run these tests in parallel
    within one process (e.g. `pytest -n`). CI runs them serially for that reason.
    """
    real = module.datetime.datetime

    class FrozenDateTime(real):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)

    with mock.patch.object(module.datetime, "datetime", FrozenDateTime), mock.patch.object(
        module.time, "time", lambda: when.timestamp()
    ):
        yield


@contextlib.contextmanager
def temp_ledger(module):
    """Point the module's ledger at a throwaway file and yield its path."""
    d = tempfile.mkdtemp(prefix="paysera-test-")
    original = module.LEDGER_FILE
    module.LEDGER_FILE = os.path.join(d, "config", "ledger.json")
    try:
        yield module.LEDGER_FILE
    finally:
        module.LEDGER_FILE = original
        # The ledger holds test IBANs and amounts, and CI runs the whole suite twice.
        # ignore_errors: a cleanup failure must not turn a passing test red.
        shutil.rmtree(d, ignore_errors=True)


class FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@contextlib.contextmanager
def capture_curl(module, stdout='{"ok":true}\nHTTP:200', returncode=0):
    """Replace subprocess.run and record every (argv, stdin) pair it was called with."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"argv": list(cmd), "input": kwargs.get("input"), "kwargs": kwargs})
        return FakeCompletedProcess(stdout=stdout, returncode=returncode)

    with mock.patch.object(module.subprocess, "run", fake_run):
        yield calls


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)
