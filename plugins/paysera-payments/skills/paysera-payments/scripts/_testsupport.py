"""Shared helpers for the script tests.

The scripts are named with hyphens (`create-payment.py`), so they cannot be imported
normally — they are loaded by path. Not named `test_*`, so no test runner collects it.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent


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
