"""lab/lab.py uses the same kube context as pfa for every command."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

LAB_PY = Path(__file__).resolve().parents[1] / "lab" / "lab.py"


@pytest.fixture
def lab(monkeypatch):
    spec = importlib.util.spec_from_file_location("lab_tool", LAB_PY)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "lab_tool", module)  # dataclasses look the module up
    spec.loader.exec_module(module)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        out = "kind-current\n" if command[1:3] == ["config", "current-context"] else ""
        return subprocess.CompletedProcess(command, 0, out, "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.calls = calls
    return module


def test_without_pfa_context_uses_kubectls_current_one(lab):
    lab.kubectl("get", "pods")
    assert lab.calls[-1] == ["kubectl", "--context", "kind-current", "get", "pods"]


def test_pfa_context_wins_so_lab_and_pfa_see_the_same_cluster(lab, monkeypatch):
    monkeypatch.setenv("PFA_KUBE_CONTEXT", "kind-pfa-dev")
    lab.kubectl("apply", "-k", "lab")
    assert lab.calls == [["kubectl", "--context", "kind-pfa-dev", "apply", "-k", "lab"]]


def test_guard_checks_the_context_actually_used(lab, monkeypatch):
    monkeypatch.setenv("PFA_KUBE_CONTEXT", "prod-eu")
    with pytest.raises(SystemExit) as exc:
        lab.guard_context()
    assert exc.value.code == 2
    assert lab.calls == []  # refused before touching any cluster
