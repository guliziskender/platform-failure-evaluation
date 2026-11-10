import pytest

from pfa.ai import rules
from pfa.engine.classifier import classify
from pfa.models import Classification, FailureType
from pfa.quantity import format_bytes, parse_quantity

from .conftest import crashed, make_view, waiting


def hits(view):
    return rules.run_rules(view, classify(view))


def best(view):
    found = hits(view)
    assert found, "expected a rule hit"
    return found[0]


# --- OOM ------------------------------------------------------------------------------------


def test_oom_with_limit_suggests_bigger_limit():
    view = make_view({**crashed(137, "OOMKilled"), "resources": {"limits": {"memory": "512Mi"}}})
    hit = best(view)
    assert (hit.rule, hit.certainty) == ("oom_memory_limit", "high")
    assert "512Mi" in hit.cause
    assert "1Gi" in hit.fix[0]


def test_oom_limit_without_unit():
    view = make_view({**crashed(137, "OOMKilled"), "resources": {"limits": {"memory": "512"}}})
    hit = best(view)
    assert hit.rule == "oom_limit_unit_typo"
    assert "512Mi" in hit.fix[0]


def test_oom_without_limit_blames_node_memory():
    hit = best(make_view(crashed(137, "OOMKilled")))
    assert (hit.rule, hit.certainty) == ("oom_no_limit", "medium")


# --- image pull -----------------------------------------------------------------------------


def _pull(message: str, **container):
    return make_view(
        {**waiting("ErrImagePull", message), **container},
        pod={"phase": "Pending"},
        events=[{"reason": "Failed", "message": f'Failed to pull image "x": {message}'}],
    )


def test_image_unauthorized():
    hit = best(_pull("failed to authorize: 401 Unauthorized"))
    assert (hit.rule, hit.certainty) == ("image_pull_unauthorized", "high")
    assert "imagePullSecrets: none" in hit.cause


def test_docker_hub_ambiguous_denied_is_medium():
    view = _pull("pull access denied, repository does not exist or may require authorization")
    hit = best(view)
    assert (hit.rule, hit.certainty) == ("image_pull_unauthorized", "medium")
    assert "image_not_found" not in [h.rule for h in hits(view)]


def test_image_rate_limited():
    assert best(_pull("429 toomanyrequests: You have reached your pull rate limit")).rule == (
        "image_pull_rate_limited"
    )


def test_registry_unreachable():
    hit = best(_pull("dial tcp: lookup registry.example.com: no such host"))
    assert hit.rule == "image_registry_unreachable"


def test_invalid_image_name():
    view = make_view({**waiting("InvalidImageName", "invalid format"), "image": "App:Latest"})
    assert best(view).rule == "invalid_image_name"


def test_image_tag_parsed_with_registry_port():
    view = _pull("not found", image="registry.local:5000/team/app:v2")
    assert best(view).rule == "image_not_found"
    assert 'tag "v2"' in best(view).cause


# --- pending --------------------------------------------------------------------------------


def _unschedulable(message: str, containers=None, **pod):
    return make_view(
        {},
        pod={
            "phase": "Pending",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": message,
                }
            ],
            **({"containers": containers} if containers else {}),
            **pod,
        },
    )


def test_insufficient_memory_without_unit_mistake():
    view = _unschedulable(
        "0/3 nodes are available: 3 Insufficient memory.",
        containers=[{"name": "app", "resources": {"requests": {"memory": "64Gi"}}}],
    )
    hit = best(view)
    assert hit.rule == "pending_insufficient_resources"
    assert "memory" in hit.cause and "64Gi" in hit.cause


def test_cpu_unit_mistake():
    view = _unschedulable(
        "0/1 nodes are available: 1 Insufficient cpu.",
        containers=[{"name": "app", "resources": {"requests": {"cpu": "2k"}}}],
    )
    hit = best(view)
    assert hit.rule == "pending_cpu_unit_typo"
    assert '"2000m"' in hit.fix[0]


def test_realistic_cpu_request_is_not_called_a_typo():
    view = _unschedulable(
        "0/1 nodes are available: 1 Insufficient cpu.",
        containers=[{"name": "app", "resources": {"requests": {"cpu": "16"}}}],
    )
    assert best(view).rule == "pending_insufficient_resources"


def test_gpu_resource_name_with_dots():
    view = _unschedulable("0/2 nodes are available: 2 Insufficient nvidia.com/gpu.")
    assert "nvidia.com/gpu" in best(view).finding


def test_node_selector_mismatch():
    view = _unschedulable(
        "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector.",
        scheduling={"node_selector": {"disktype": "ssd"}},
    )
    hit = best(view)
    assert hit.rule == "pending_node_selector"
    assert "disktype" in hit.cause


def test_untolerated_taint():
    view = _unschedulable(
        "0/1 nodes are available: 1 node(s) had untolerated taint "
        "{node-role.kubernetes.io/control-plane: }."
    )
    hit = best(view)
    assert hit.rule == "pending_untolerated_taint"
    assert "control-plane" in hit.cause


def test_unbound_pvc():
    view = _unschedulable(
        "0/1 nodes are available: pod has unbound immediate PersistentVolumeClaims.",
        pvcs=["data-db-0"],
    )
    hit = best(view)
    assert hit.rule == "pending_pvc"
    assert "data-db-0" in hit.cause


def test_failed_mount_missing_configmap():
    view = make_view(
        waiting("ContainerCreating"),
        pod={"phase": "Pending"},
        events=[
            {
                "reason": "FailedMount",
                "message": 'MountVolume.SetUp failed for volume "cfg" : configmap "app-config" '
                "not found",
            }
        ],
    )
    hit = best(view)
    assert (hit.rule, hit.certainty) == ("pending_failed_mount", "high")
    assert "app-config" in hit.cause


# --- probes ---------------------------------------------------------------------------------


def _probe_view(message: str, probe: dict, ports=("http=80/TCP",), kind="Liveness"):
    return make_view(
        {**crashed(137), "ports": list(ports), "probes": {kind.lower(): probe}},
        events=[
            {"reason": "Unhealthy", "message": f"{kind} probe failed: {message}"},
            {
                "type": "Normal",
                "reason": "Killing",
                "message": f"Container app failed {kind.lower()} probe, will be restarted",
            },
        ],
    )


def test_probe_named_port_resolves():
    view = _probe_view(
        "dial tcp 10.0.0.1:80: connect: connection refused",
        {"http_get": "HTTP /healthz port=http"},
    )
    found = [h.rule for h in hits(view)]
    assert "probe_wrong_port" not in found
    assert found[0] == "probe_app_not_listening"


def test_probe_404_means_wrong_path():
    view = _probe_view(
        "HTTP probe failed with statuscode: 404", {"http_get": "HTTP /health port=80"}
    )
    hit = best(view)
    assert (hit.rule, hit.certainty) == ("probe_http_status", "high")
    assert "/health" in hit.cause


def test_probe_500_is_medium():
    view = _probe_view(
        "HTTP probe failed with statuscode: 503", {"http_get": "HTTP /ready port=80"}
    )
    assert best(view).certainty == "medium"


def test_probe_timeout():
    view = _probe_view(
        'Get "http://10.0.0.1:80/healthz": context deadline exceeded',
        {"http_get": "HTTP /healthz port=80", "timeout_seconds": 1},
    )
    hit = best(view)
    assert hit.rule == "probe_timeout"
    assert "timeoutSeconds=1" in hit.cause


# --- crashes --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "log",
    [
        "FATAL: required environment variable DATABASE_URL is not set",
        "Error: missing required environment variable: REDIS_HOST",
        "config error: env var STRIPE_KEY is required",
        "KeyError: 'SECRET_KEY'",
        "PORT environment variable is not set",
    ],
)
def test_missing_env_var_patterns(log):
    hit = best(make_view({**crashed(1), "env_names": ["LOG_LEVEL"]}, logs=log))
    assert (hit.rule, hit.certainty) == ("crash_missing_env_var", "high")


def test_missing_env_var_that_is_defined_is_medium():
    view = make_view({**crashed(1), "env_names": ["DATABASE_URL"]}, logs="DATABASE_URL is not set")
    hit = best(view)
    assert hit.certainty == "medium"
    assert "empty" in hit.cause


def test_missing_env_var_with_env_from_is_medium():
    view = make_view(
        {**crashed(1), "env_from": ["configmap/app"]},
        logs="environment variable API_URL is not set",
    )
    hit = best(view)
    assert hit.certainty == "medium"
    assert "configmap/app" in hit.cause


def test_missing_secret_reference():
    view = make_view(
        waiting("CreateContainerConfigError", 'secret "db-creds" not found'),
        pod={"phase": "Pending"},
    )
    hit = best(view)
    assert hit.rule == "crash_missing_config_object"
    assert "db-creds" in hit.cause


def test_missing_configmap_key():
    msg = "couldn't find key DB_HOST in ConfigMap default/app-config"
    view = make_view(waiting("CreateContainerConfigError", msg), pod={"phase": "Pending"})
    assert "DB_HOST" in best(view).cause


def test_command_not_found_exit_127():
    view = make_view({**crashed(127), "command": ["/app/server"]})
    hit = best(view)
    assert hit.rule == "crash_command_not_found"
    assert "/app/server" in hit.cause


def test_command_not_found_runtime_message():
    msg = 'exec: "bash": executable file not found in $PATH'
    view = make_view({**crashed(128, reason="StartError", message=msg)})
    assert best(view).rule == "crash_command_not_found"


def test_permission_denied():
    assert best(make_view(crashed(126))).rule == "crash_permission_denied"


def test_process_exits_zero_in_deployment():
    hit = best(make_view(crashed(0, reason="Completed")))
    assert hit.rule == "crash_exits_successfully"


def test_segfault():
    assert best(make_view(crashed(139))).rule == "crash_signal"


def test_log_error_fallback_is_low():
    logs = "=== previous container ===\nstarting\npanic: runtime error: index out of range\n"
    hit = best(make_view(crashed(2), logs=logs))
    assert (hit.rule, hit.certainty) == ("crash_log_error", "low")
    assert "index out of range" in hit.cause


def test_no_output_crash():
    assert best(make_view(crashed(3))).rule == "crash_no_output"


# --- workloads, answers ---------------------------------------------------------------------


def test_quota_failed_create():
    view = make_view(
        has_pod=False,
        events=[
            {
                "reason": "FailedCreate",
                "message": 'pods "app-1" is forbidden: exceeded quota: compute, requested: cpu=2',
            }
        ],
    )
    hit = best(view)
    assert hit.rule == "workload_failed_create"
    assert "ResourceQuota" in hit.cause


def test_matching_failure_type_ranks_first():
    # A low-certainty crash hit must not beat a high-certainty hit of the classified type.
    view = make_view({**crashed(1), "env_names": []}, logs="FATAL: DATABASE_URL is not set")
    assert [h.rule for h in hits(view)][:2] == ["crash_missing_env_var", "crash_log_error"]


def test_no_hit_answers_unsure():
    classification = Classification(failure_type=FailureType.UNKNOWN, problem="?")
    diagnosis = rules.answer_from_rules(classification, [])
    assert diagnosis.confidence == "unsure"
    assert "not known" in diagnosis.likely_cause


def test_unknown_classification_takes_rule_failure_type():
    view = make_view(
        has_pod=False,
        events=[{"reason": "FailedCreate", "message": "violates PodSecurity restricted"}],
    )
    report_type = rules.answer_from_rules(classify(view), hits(view)).failure_type
    assert report_type is FailureType.UNKNOWN


def test_every_rule_has_a_unique_name():
    names = [r.__name__ for r in rules.RULES]
    assert len(names) == len(set(names))


# --- quantities -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "value"),
    [("500m", 0.5), ("4k", 4000), ("2", 2), ("128Mi", 128 * 2**20), ("1G", 1e9), ("1e3", 1000)],
)
def test_parse_quantity(raw, value):
    assert parse_quantity(raw) == value


def test_parse_quantity_rejects_garbage():
    assert parse_quantity("lots") is None
    assert parse_quantity(None) is None


def test_format_bytes():
    assert format_bytes(256 * 2**20) == "256Mi"
    assert format_bytes(2**30) == "1Gi"


MIRROR_500 = (
    'failed to resolve reference "registry.pfa-lab.invalid/billing/ui:3.2.0": unexpected status '
    "from HEAD request to http://registry-mirror:1273/v2/billing/ui/manifests/3.2.0"
    "?ns=registry.pfa-lab.invalid: 500 Internal Server Error"
)


def test_mirror_server_error_names_the_mirror_and_the_registry():
    hit = best(_pull(MIRROR_500))
    assert (hit.rule, hit.certainty) == ("image_registry_unreachable", "medium")
    assert "registry mirror registry-mirror:1273" in hit.cause
    assert "registry registry.pfa-lab.invalid" in hit.cause
    assert '"500 Internal Server Error"' in hit.cause
    assert "registry.pfa-lab.invalid" in hit.fix[0]


def test_registry_server_error_without_a_mirror():
    message = "unexpected status from GET request to https://reg.example.com/v2/: "
    hit = best(_pull(message + "503 Service Unavailable"))
    assert hit.rule == "image_registry_unreachable"
    assert hit.cause.startswith('The registry answered "503 Service Unavailable"')


def test_not_found_is_not_mistaken_for_a_server_error():
    assert best(_pull("failed to resolve reference: x:1: not found")).rule == "image_not_found"


def test_crash_log_rule_names_a_failing_init_container():
    container = crashed(1)
    main = {"name": "web", **waiting("PodInitializing")}
    pod = {
        "phase": "Pending",
        "init_containers": [{"name": "app", **container}],
        "containers": [main],
    }
    view = make_view(container, pod=pod, logs='ERROR: relation "orders_idx" already exists')
    hit = best(view)
    assert hit.rule == "crash_log_error"
    assert hit.cause.startswith("Init container app fails, so the app containers never start")
    assert "already exists" in hit.cause
