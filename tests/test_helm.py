"""The Helm chart: read-only RBAC that covers exactly what pfa calls, and a locked-down pod.

Renders the chart with `helm template` (skipped if helm is not installed). The live install is
tests/integration/test_helm_install.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "pfa"
READ_VERBS = {"get", "list", "watch"}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def render(*sets: str) -> list[dict]:
    args = ["helm", "template", "t", str(CHART), "-n", "pfa"]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def render_error(*sets: str) -> str:
    args = ["helm", "template", "t", str(CHART)]
    for s in sets:
        args += ["--set", s]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode != 0
    return result.stderr


def kinds(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def pod_specs(docs: list[dict]) -> list[dict]:
    return [d["spec"]["template"]["spec"] for d in docs if d["kind"] in ("Deployment", "Job")]


def granted(docs: list[dict]) -> set[tuple[str, str, str]]:
    """(apiGroup, resource, verb) for every rule in every Role and ClusterRole."""
    return {
        (group, resource, verb)
        for role in kinds(docs, "ClusterRole") + kinds(docs, "Role")
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
    }


# --- RBAC -------------------------------------------------------------------------------------


def test_rbac_is_read_only_and_never_touches_secrets():
    rules = granted(render())
    assert rules
    assert {verb for _, _, verb in rules} <= READ_VERBS
    assert not {r for _, r, _ in rules} & {"secrets", "configmaps", "pods/exec", "*"}


# Each Kubernetes client method pfa calls, as the RBAC permission it needs.
_METHOD = re.compile(r"clients\.(core|apps)\.(read|list)_namespaced_(\w+)")
_RESOURCE = {
    "pod": "pods",
    "pod_log": "pods/log",
    "event": "events",
    "deployment": "deployments",
    "replica_set": "replicasets",
    "stateful_set": "statefulsets",
}


def calls_in_source() -> set[tuple[str, str, str]]:
    needed = set()
    for path in (ROOT / "src" / "pfa").rglob("*.py"):
        for api, action, thing in _METHOD.findall(path.read_text()):
            assert thing in _RESOURCE, f"{path.name}: unknown API call {action}_namespaced_{thing}"
            group = "" if api == "core" else "apps"
            needed.add((group, _RESOURCE[thing], "get" if action == "read" else "list"))
    return needed


def test_rbac_allows_every_api_call_pfa_makes():
    # A new API call in pfa fails here until the chart allows it (and the README says so).
    needed = calls_in_source()
    assert len(needed) >= 7, needed  # the scan itself works
    assert needed <= granted(render())


def test_rbac_grants_nothing_pfa_does_not_use():
    used = {(g, r) for g, r, _ in calls_in_source()}
    assert {(g, r) for g, r, _ in granted(render())} == used


def test_namespaced_scope_has_a_role_per_namespace_and_no_cluster_role():
    docs = render("rbac.scope=namespaces", "rbac.namespaces={shop,payments}")
    assert not kinds(docs, "ClusterRole") and not kinds(docs, "ClusterRoleBinding")
    assert {r["metadata"]["namespace"] for r in kinds(docs, "Role")} == {"shop", "payments"}
    for binding in kinds(docs, "RoleBinding"):
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "t-pfa", "namespace": "pfa"}
        ]
    assert granted(docs) == granted(render())  # same rules, smaller reach


# --- pod security -------------------------------------------------------------------------------


@pytest.mark.parametrize("sets", [(), ("diagnose.name=checkout-api",)], ids=["toolbox", "job"])
def test_pods_are_locked_down(sets):
    specs = pod_specs(render(*sets))
    assert specs
    for spec in specs:
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        (container,) = spec["containers"]
        sc = container["securityContext"]
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["capabilities"]["drop"] == ["ALL"]
        assert "limits" in container["resources"]


# --- data and secrets ---------------------------------------------------------------------------


def config_of(docs: list[dict]) -> dict:
    (cm,) = kinds(docs, "ConfigMap")
    return yaml.safe_load(cm["data"]["config.yaml"])


def test_default_sends_nothing():
    docs = render()
    assert config_of(docs) == {"provider": "none", "send_data": False}
    for spec in pod_specs(docs):
        assert [e["name"] for e in spec["containers"][0]["env"]] == ["PFA_CONFIG"]  # no key


def test_api_key_only_ever_comes_from_a_secret_the_user_created():
    docs = render("ai.provider=anthropic", "ai.existingSecret=my-key", "ai.sendData=true")
    assert not kinds(docs, "Secret")  # the chart never holds the key
    (env,) = [
        e for e in pod_specs(docs)[0]["containers"][0]["env"] if e["name"] == "ANTHROPIC_API_KEY"
    ]
    assert env == {
        "name": "ANTHROPIC_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "my-key", "key": "ANTHROPIC_API_KEY"}},
    }
    assert "anthropic_api_key" not in config_of(docs)


def test_rendered_config_is_valid_for_pfa(tmp_path, monkeypatch):
    # pfa's config loader is strict: a key it does not know would fail at startup in the pod.
    from pfa.config import Settings

    docs = render(
        "ai.provider=ollama",
        "ai.model=qwen3:8b",
        "ai.ollamaUrl=http://ollama.ollama.svc:11434",
        "ai.timeoutSeconds=90",
        "ai.sendData=true",
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_of(docs)))
    monkeypatch.setenv("PFA_CONFIG", str(path))
    s = Settings.load()
    assert (s.provider, s.model, s.ollama_url, s.llm_timeout, s.send_data) == (
        "ollama",
        "qwen3:8b",
        "http://ollama.ollama.svc:11434",
        90.0,
        True,
    )


# --- the diagnose Job -----------------------------------------------------------------------


def test_no_job_unless_a_target_is_given():
    assert not kinds(render(), "Job")


def test_job_runs_the_diagnosis_and_is_named_after_it():
    (job,) = kinds(
        render("diagnose.name=checkout-api", "diagnose.namespace=shop", "diagnose.noLlm=true"),
        "Job",
    )
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args == ["diagnose", "checkout-api", "-n", "shop", "-o", "text", "--no-llm"]
    assert job["spec"]["backoffLimit"] == 0
    (other,) = kinds(render("diagnose.name=orders-api"), "Job")
    assert other["metadata"]["name"] != job["metadata"]["name"]  # Jobs cannot be edited


# --- values that would install something broken fail at render time ---------------------------


@pytest.mark.parametrize(
    ("sets", "message"),
    [
        (("ai.provider=anthropic",), "needs ai.existingSecret"),
        (("ai.provider=gemini",), "must be none, anthropic or ollama"),
        (("rbac.scope=namespaces",), "needs rbac.namespaces"),
        (("rbac.scope=everything",), "must be cluster or namespaces"),
    ],
)
def test_bad_values_fail_with_a_clear_message(sets, message):
    assert message in render_error(*sets)
