"""Install the Helm chart into a local cluster and diagnose a lab case from inside it.

Run: make test-helm (builds the image, needs the lab: make lab-up). Checks what the service
account may do with `kubectl auth can-i`, i.e. the API server's own answer, not the chart's text.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .require import unavailable

ROOT = Path(__file__).resolve().parents[2]
RELEASE = NAMESPACE = "pfa-helm-test"
SA = f"system:serviceaccount:{NAMESPACE}:{RELEASE}"

pytestmark = [pytest.mark.integration, pytest.mark.helm]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=300, check=check)


def helm_install(*sets: str) -> None:
    args = ["helm", "upgrade", "--install", RELEASE, str(ROOT / "deploy/helm/pfa")]
    args += ["-n", NAMESPACE, "--create-namespace", "--wait", "--timeout", "120s"]
    for s in ("image.repository=pfa", "image.tag=dev", "image.pullPolicy=Never", *sets):
        args += ["--set", s]
    run(*args)


def exec_pfa(*args: str) -> subprocess.CompletedProcess:
    return run(
        "kubectl", "-n", NAMESPACE, "exec", f"deploy/{RELEASE}", "--", "pfa", *args, check=False
    )


def can_i(verb: str, resource: str, namespace: str) -> bool:
    out = run("kubectl", "auth", "can-i", verb, resource, "-n", namespace, "--as", SA, check=False)
    return out.stdout.strip() == "yes"


@pytest.fixture(scope="module", autouse=True)
def installed():
    if not shutil.which("helm") or not shutil.which("docker"):
        unavailable("needs helm and docker")
    if run("kubectl", "get", "ns", "pfa-lab", check=False).returncode != 0:
        unavailable("needs the lab: make lab-up")
    # A previous run deletes its namespace without waiting; installing into it while it
    # terminates fails. Back-to-back runs wait for it here.
    run("kubectl", "wait", "--for=delete", f"namespace/{NAMESPACE}", "--timeout=180s", check=False)
    load = run(str(ROOT / "deploy/load-image.sh"), check=False)
    if load.returncode != 0:
        unavailable(f"cannot load the image: {load.stderr.strip()}")
    helm_install()
    yield
    run("helm", "uninstall", RELEASE, "-n", NAMESPACE, "--wait", check=False)
    run("kubectl", "delete", "namespace", NAMESPACE, "--wait=false", check=False)


def test_diagnoses_a_lab_case_from_inside_the_cluster():
    result = exec_pfa("diagnose", "checkout-api", "-n", "pfa-lab", "--no-llm", "-o", "json")
    assert result.returncode == 0, result.stderr
    d = json.loads(result.stdout)["diagnosis"]
    assert (d["failure_type"], d["confidence"]) == ("crash_loop_backoff", "high")
    assert "DATABASE_URL" in d["likely_cause"]
    assert "pfa-fake-secret-value" not in result.stdout


@pytest.mark.parametrize(
    ("verb", "resource"),
    [
        ("get", "pods"),
        ("get", "pods/log"),
        ("list", "events"),
        ("get", "deployments.apps"),
        ("list", "replicasets.apps"),
        ("get", "statefulsets.apps"),
        ("get", "persistentvolumeclaims"),
    ],
)
def test_service_account_can_read_what_pfa_needs(verb, resource):
    assert can_i(verb, resource, "pfa-lab")


@pytest.mark.parametrize(
    ("verb", "resource"),
    [
        ("get", "secrets"),
        ("list", "secrets"),
        ("get", "configmaps"),
        ("delete", "pods"),
        ("create", "pods/exec"),
        ("patch", "deployments.apps"),
        ("update", "deployments.apps/scale"),
        ("delete", "persistentvolumeclaims"),
        ("get", "persistentvolumes"),
    ],
)
def test_service_account_cannot_do_anything_else(verb, resource):
    assert not can_i(verb, resource, "pfa-lab")
    assert not can_i(verb, resource, NAMESPACE)  # not even in its own namespace


def test_namespaced_scope_reaches_only_the_listed_namespaces():
    helm_install("rbac.scope=namespaces", "rbac.namespaces={pfa-lab}")
    try:
        assert can_i("list", "pods", "pfa-lab")
        assert not can_i("list", "pods", "default")
        refused = exec_pfa("diagnose", "anything", "-n", "default", "--no-llm")
        assert refused.returncode == 2
        assert "no permission to read namespace 'default'" in refused.stderr
    finally:
        helm_install()  # back to the default for any later test


_OUTBOUND = """
import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=4)
    print("open")
except OSError:
    print("blocked")
"""
_DNS = "import socket; socket.getaddrinfo('kubernetes.default.svc', 443); print('resolved')"


def _python(code: str) -> str:
    out = run(
        "kubectl",
        "-n",
        NAMESPACE,
        "exec",
        f"deploy/{RELEASE}",
        "--",
        "python",
        "-c",
        code,
        check=False,
    )
    return out.stdout.strip()


def _api_server_cidrs() -> str:
    out = run(
        "kubectl",
        "get",
        "endpointslice",
        "-n",
        "default",
        "-l",
        "kubernetes.io/service-name=kubernetes",
        "-o",
        "jsonpath={.items[0].endpoints[*].addresses[*]}",
    )
    return "{" + ",".join(f"{ip}/32" for ip in out.stdout.split()) + "}"


def test_network_policy_allows_the_api_and_dns_and_blocks_the_rest():
    if _python(_OUTBOUND) != "open":
        pytest.skip("this cluster has no outbound internet, so a block would prove nothing")
    helm_install(
        "networkPolicy.enabled=true", f"networkPolicy.apiServer.cidrs={_api_server_cidrs()}"
    )
    try:
        run("kubectl", "-n", NAMESPACE, "rollout", "status", f"deploy/{RELEASE}")
        diagnosed = exec_pfa("diagnose", "checkout-api", "-n", "pfa-lab", "--no-llm", "-o", "json")
        assert diagnosed.returncode == 0, diagnosed.stderr  # the API server is reachable
        assert _python(_DNS) == "resolved"
        if _python(_OUTBOUND) == "open":
            unavailable("the network plugin does not enforce NetworkPolicy")
        assert _python(_OUTBOUND) == "blocked"
    finally:
        helm_install()
