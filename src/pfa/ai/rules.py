"""Deterministic rules: answer obvious cases instantly, without an LLM.

Each rule looks for one specific, checkable signal and returns a RuleHit, or None. Hits are
the whole answer under --no-llm and become hints for the model later. A rule only claims
"high" certainty when the evidence pins down the cause, not just the symptom.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from pfa.engine.classifier import KILLED_EXIT_CODES
from pfa.models import Certainty, Classification, Diagnosis, FailureType, RuleHit
from pfa.quantity import format_bytes, parse_quantity
from pfa.view import EvidenceView

Rule = Callable[[EvidenceView], RuleHit | None]
RULES: list[Rule] = []

_CERTAINTY_ORDER = {"high": 0, "medium": 1, "low": 2}


def rule(func: Rule) -> Rule:
    RULES.append(func)
    return func


def run_rules(view: EvidenceView, classification: Classification) -> list[RuleHit]:
    """All matching rules, best first: same failure type as the classification, then certainty."""
    hits = [h for r in RULES if (h := r(view)) is not None]
    return sorted(
        hits,
        key=lambda h: (
            h.failure_type is not classification.failure_type,
            _CERTAINTY_ORDER[h.certainty],
        ),
    )


def answer_from_rules(classification: Classification, hits: list[RuleHit]) -> Diagnosis:
    """The --no-llm answer: the best rule hit, or an honest "unsure"."""
    if not hits:
        if classification.failure_type is FailureType.NONE:
            return Diagnosis(
                failure_type=FailureType.NONE,
                problem=classification.problem,
                likely_cause="Nothing is failing right now.",
                fix=[],
                confidence="high",
                evidence_used=classification.signals,
            )
        return Diagnosis(
            failure_type=classification.failure_type,
            problem=classification.problem,
            likely_cause="No deterministic rule matched this failure, so the cause is not known.",
            fix=[
                "Inspect the collected evidence: re-run with --show-evidence",
                "Check the full container logs: kubectl logs <pod> -c <container> --previous",
            ],
            confidence="unsure",
            evidence_used=classification.signals,
        )
    best = hits[0]
    failure_type = classification.failure_type
    if failure_type in (FailureType.UNKNOWN, FailureType.NONE):
        failure_type = best.failure_type
    return Diagnosis(
        failure_type=failure_type,
        problem=classification.problem,
        likely_cause=best.cause,
        fix=best.fix,
        confidence=best.certainty,
        evidence_used=_dedupe(best.evidence + classification.signals),
        rule_hits=[h.rule for h in hits],
    )


# =============================================================================================
# OOMKilled
# =============================================================================================


@rule
def oom_memory_limit(v: EvidenceView) -> RuleHit | None:
    term = next(
        (t for t in (v.terminated, v.last_terminated) if t.get("reason") == "OOMKilled"), None
    )
    if term is None:
        return None
    resources = v.container.get("resources", {})
    limit = resources.get("limits", {}).get("memory")
    evidence = [f"termination: OOMKilled, exit {term.get('exit_code')}", f"restarts: {v.restarts}"]

    if limit is None:
        return _hit(
            "oom_no_limit",
            FailureType.OOM_KILLED,
            finding=f"Container {v.container_name} was OOMKilled but has no memory limit.",
            cause="With no memory limit, the container was killed because the node itself ran "
            "out of memory; this pod (or its neighbours) used more than the node could give.",
            fix=[
                "Set resources.requests.memory to the app's real usage so the scheduler "
                "reserves enough memory for it",
                "Set resources.limits.memory so one runaway pod cannot exhaust the node",
            ],
            certainty="medium",
            evidence=evidence + ["no memory limit"],
        )

    evidence.append(f"memory limit: {limit}")
    limit_bytes = parse_quantity(limit) or 0
    if limit_bytes < 16 * 2**20:  # < 16Mi: nothing real runs in that; almost always a unit typo
        return _hit(
            "oom_limit_unit_typo",
            FailureType.OOM_KILLED,
            finding=f"Memory limit of container {v.container_name} is only {limit}.",
            cause=f"The memory limit is {limit} ({int(limit_bytes)} bytes), far too small for any "
            f"process; the unit is probably missing (e.g. {limit} instead of {limit}Mi).",
            fix=[f"Write the limit with a unit, e.g. resources.limits.memory: {limit}Mi"],
            certainty="high",
            evidence=evidence,
        )
    suggested = format_bytes(_round_up_mi(limit_bytes * 2))
    return _hit(
        "oom_memory_limit",
        FailureType.OOM_KILLED,
        finding=f"Container {v.container_name} was OOMKilled {v.restarts} times; its memory "
        f"limit is {limit}.",
        cause=f"The process needs more memory than its {limit} memory limit, so the kernel "
        "kills it each time it reaches the limit. Either the limit is too low for the workload "
        "or the app is leaking memory.",
        fix=[
            f"Raise resources.limits.memory above the app's real peak usage, e.g. to {suggested}"
            " (check usage with kubectl top pod or your metrics)",
            "If memory grows without bound, look for a leak or make the app load less at once "
            "(cache size, batch size, JVM -Xmx / heap settings)",
        ],
        certainty="high",
        evidence=evidence,
    )


# =============================================================================================
# ImagePullBackOff
# =============================================================================================


def _pull_messages(v: EvidenceView) -> str:
    parts = [v.waiting.get("message", "")]
    parts += [e["message"] for e in v.events_where("Failed", "Failed to pull image")]
    return "\n".join(p for p in parts if p)


def _pull_evidence(v: EvidenceView, needle: str) -> list[str]:
    image = v.container.get("image")
    lines = [f"image: {image}"] if image else []
    for e in v.events_where("Failed", "Failed to pull image"):
        if needle.lower() in e["message"].lower():
            lines.append(f"event Failed x{e.get('count', 1)}: {e['message']}")
            break
    return lines


@rule
def image_not_found(v: EvidenceView) -> RuleHit | None:
    messages = _pull_messages(v)
    if not re.search(r"\bnot found\b|manifest unknown|NotFound", messages):
        return None
    if "may require authorization" in messages:
        return None  # ambiguous on Docker Hub: handled by image_pull_unauthorized
    image = v.container.get("image", "?")
    repo, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "latest")
    return _hit(
        "image_not_found",
        FailureType.IMAGE_PULL,
        finding=f'The registry answered "not found" for image {image}.',
        cause=f'The registry answers "not found" for {image}: the image does not exist. The '
        f'tag "{tag}" (or the repository name) is wrong, or that version was never pushed.',
        fix=[
            f"Check which tags exist for {repo} (registry UI, `crane ls {repo}`, or "
            f"`docker manifest inspect {image}`)",
            "Correct the tag in the workload's container image and roll out again",
        ],
        certainty="high",
        evidence=_pull_evidence(v, "not found"),
    )


@rule
def image_pull_unauthorized(v: EvidenceView) -> RuleHit | None:
    messages = _pull_messages(v)
    if not re.search(
        r"unauthorized|authentication required|access denied|denied|403 Forbidden", messages, re.I
    ):
        return None
    secrets = v.pod.get("image_pull_secrets", [])
    secrets_text = ", ".join(secrets) if secrets else "none"
    ambiguous = "repository does not exist or may require authorization" in messages
    cause = (
        "The registry refused the pull. Either the repository name is wrong or it is private "
        f"and the pod has no valid credentials (imagePullSecrets: {secrets_text})."
        if ambiguous
        else "The registry refused the pull: the image is private and the pod has no valid "
        f"credentials for it (imagePullSecrets: {secrets_text})."
    )
    return _hit(
        "image_pull_unauthorized",
        FailureType.IMAGE_PULL,
        finding="The registry denied access to the image.",
        cause=cause,
        fix=[
            "Create a registry secret: kubectl create secret docker-registry <name> "
            "--docker-server=<registry> --docker-username=... --docker-password=...",
            "Reference it in spec.imagePullSecrets (or attach it to the pod's ServiceAccount)",
            "If the secret already exists, check it has not expired and is for this registry",
        ]
        + (["Double-check the repository name for typos"] if ambiguous else []),
        certainty="medium" if ambiguous else "high",
        evidence=_pull_evidence(v, "denied") or _pull_evidence(v, "unauthorized"),
    )


@rule
def image_pull_rate_limited(v: EvidenceView) -> RuleHit | None:
    if "toomanyrequests" not in _pull_messages(v).lower():
        return None
    return _hit(
        "image_pull_rate_limited",
        FailureType.IMAGE_PULL,
        finding="The registry answered toomanyrequests.",
        cause="The registry is rate-limiting pulls (typical for anonymous Docker Hub pulls).",
        fix=[
            "Authenticate pulls with an imagePullSecret (higher limits)",
            "Mirror the image to your own registry, or use a pull-through cache",
        ],
        certainty="high",
        evidence=_pull_evidence(v, "toomanyrequests"),
    )


@rule
def image_registry_unreachable(v: EvidenceView) -> RuleHit | None:
    m = re.search(
        r"no such host|i/o timeout|connection refused|TLS handshake timeout|"
        r"x509: certificate|context deadline exceeded",
        _pull_messages(v),
    )
    if not m:
        return None
    return _hit(
        "image_registry_unreachable",
        FailureType.IMAGE_PULL,
        finding=f"Pulling the image failed with a network error ({m.group(0)}).",
        cause="The node cannot reach the registry: wrong registry hostname, DNS, firewall or "
        "proxy rules, or an untrusted TLS certificate.",
        fix=[
            "Check the registry hostname in the image reference",
            "From a node, test reachability of the registry (DNS, proxy, firewall)",
            "For a private CA, add it to the container runtime's trusted certificates",
        ],
        certainty="medium",
        evidence=_pull_evidence(v, m.group(0)),
    )


@rule
def invalid_image_name(v: EvidenceView) -> RuleHit | None:
    if v.waiting.get("reason") != "InvalidImageName":
        return None
    image = v.container.get("image", "?")
    return _hit(
        "invalid_image_name",
        FailureType.IMAGE_PULL,
        finding=f"The image reference {image!r} is not valid.",
        cause=f"The image reference {image!r} is not valid syntax (e.g. uppercase letters, "
        "spaces, or an empty variable substituted into it).",
        fix=["Fix the image field: registry/repository:tag, lowercase, no spaces"],
        certainty="high",
        evidence=[f"container waiting: InvalidImageName ({v.waiting.get('message', '')})"],
    )


# =============================================================================================
# Pending
# =============================================================================================


def _scheduler_message(v: EvidenceView) -> str:
    scheduled = v.condition("PodScheduled")
    if scheduled and scheduled["status"] == "False" and scheduled.get("message"):
        return scheduled["message"]
    events = v.events_where("FailedScheduling")
    return events[0]["message"] if events else ""


def _total_request(v: EvidenceView, resource: str) -> tuple[float, list[str]]:
    total, raw = 0.0, []
    for c in v.pod.get("containers", []):
        value = c.get("resources", {}).get("requests", {}).get(resource)
        if value is not None:
            total += parse_quantity(value) or 0
            raw.append(f"{c['name']}: {resource}={value}")
    return total, raw


@rule
def pending_insufficient_resources(v: EvidenceView) -> RuleHit | None:
    message = _scheduler_message(v)
    # Resource names may contain dots (nvidia.com/gpu); the sentence's full stop must not stick.
    resources = sorted(set(re.findall(r"Insufficient ([\w./-]*[\w/-])", message)))
    if not resources:
        return None
    evidence = [f"scheduler: {message}"]

    if "cpu" in resources:
        cores, raw = _total_request(v, "cpu")
        evidence += [f"requests {r}" for r in raw]
        if cores >= 100:  # nobody runs 100-core pods by accident; a missing "m" is likely
            millis = int(cores)
            return _hit(
                "pending_cpu_unit_typo",
                FailureType.PENDING,
                finding=f"The pod requests {cores:g} CPU cores, more than any node has.",
                cause=f"The CPU request is {cores:g} cores (shown as {', '.join(raw)}). That is "
                f'almost certainly a unit mistake: "{millis}" means {millis} whole cores, while '
                f'"{millis}m" means {millis} millicores ({millis / 1000:g} cores).',
                fix=[
                    f'Change the CPU request to "{millis}m" (or whatever the app really needs)',
                    "Keep requests within what one node can offer, or the pod never schedules",
                ],
                certainty="high",
                evidence=evidence,
            )

    asked = []
    for res in resources:
        total, raw = _total_request(v, res)
        if raw:
            asked.append(f"{res} {', '.join(raw)}")
            evidence += [f"requests {r}" for r in raw if f"requests {r}" not in evidence]
    asked_text = f" (requests: {'; '.join(asked)})" if asked else ""
    return _hit(
        "pending_insufficient_resources",
        FailureType.PENDING,
        finding=f"No node has enough free {', '.join(resources)} for this pod.",
        cause=f"The pod's {' and '.join(resources)} request is larger than the free capacity of "
        f"every node{asked_text}, so the scheduler cannot place it.",
        fix=[
            f"Lower the pod's {', '.join(resources)} requests to what the app really needs",
            "Or add capacity: more or bigger nodes (check the cluster autoscaler), or free "
            "resources used by other pods",
        ],
        certainty="high",
        evidence=evidence,
    )


@rule
def pending_node_selector(v: EvidenceView) -> RuleHit | None:
    message = _scheduler_message(v)
    if "didn't match Pod's node affinity/selector" not in message:
        return None
    scheduling = v.pod.get("scheduling", {})
    selector = scheduling.get("node_selector")
    shown = f"nodeSelector {selector}" if selector else "node affinity rules"
    return _hit(
        "pending_node_selector",
        FailureType.PENDING,
        finding="No node matches the pod's node selector / affinity.",
        cause=f"The pod's {shown} match no node in the cluster, so it can never be scheduled.",
        fix=[
            "Compare with node labels: kubectl get nodes --show-labels",
            "Fix the nodeSelector / affinity, or label the intended nodes",
        ],
        certainty="high",
        evidence=[f"scheduler: {message}"] + ([f"nodeSelector: {selector}"] if selector else []),
    )


@rule
def pending_untolerated_taint(v: EvidenceView) -> RuleHit | None:
    message = _scheduler_message(v)
    taints = re.findall(r"untolerated taint\(?s?\)? ?(\{[^}]*\})", message)
    if "untolerated taint" not in message:
        return None
    shown = ", ".join(sorted(set(taints))) or "a taint"
    return _hit(
        "pending_untolerated_taint",
        FailureType.PENDING,
        finding=f"Nodes have taints the pod does not tolerate: {shown}.",
        cause=f"The available nodes carry {shown}, and the pod has no matching toleration, so "
        "the scheduler keeps it off them.",
        fix=[
            "If the pod should run there, add a matching toleration to the pod spec",
            "Otherwise make sure untainted nodes with enough capacity exist",
        ],
        certainty="high",
        evidence=[f"scheduler: {message}"],
    )


@rule
def pending_pvc(v: EvidenceView) -> RuleHit | None:
    message = _scheduler_message(v)
    if not re.search(
        r"unbound immediate PersistentVolumeClaims|persistentvolumeclaim .* not found", message
    ):
        return None
    pvcs = ", ".join(v.pod.get("pvcs", [])) or "its PVC"
    return _hit(
        "pending_pvc",
        FailureType.PENDING,
        finding=f"The pod waits for a PersistentVolumeClaim ({pvcs}).",
        cause=f"The pod uses {pvcs}, which is missing or not bound to a volume (no matching "
        "PersistentVolume, or the StorageClass cannot provision one).",
        fix=[
            f"Check the claim: kubectl get pvc {pvcs.split(', ')[0]} and kubectl describe pvc",
            "Make sure the StorageClass exists and its provisioner is running",
        ],
        certainty="high",
        evidence=[f"scheduler: {message}"],
    )


@rule
def pending_failed_mount(v: EvidenceView) -> RuleHit | None:
    events = v.events_where("FailedMount")
    if not events:
        return None
    message = events[0]["message"]
    missing = re.search(r'(configmap|secret) "([^"]+)" not found', message, re.I)
    if missing:
        kind, name = missing.group(1).lower(), missing.group(2)
        cause = f"The pod mounts {kind} {name!r}, which does not exist in the namespace."
        fix = [f"Create the {kind} {name!r}, or fix its name in the pod's volumes"]
        certainty: Certainty = "high"
    else:
        cause = "A volume cannot be mounted, so the containers cannot start."
        fix = ["Read the FailedMount message and check the volume it names"]
        certainty = "medium"
    return _hit(
        "pending_failed_mount",
        FailureType.PENDING,
        finding="A volume of the pod fails to mount.",
        cause=cause,
        fix=fix,
        certainty=certainty,
        evidence=[f"event FailedMount x{events[0].get('count', 1)}: {message}"],
    )


# =============================================================================================
# Failing probes
# =============================================================================================

_PROBE_EVENT = re.compile(r"^(Liveness|Readiness|Startup) probe failed: (.*)", re.S)


def _probe_failures(v: EvidenceView) -> list[tuple[str, str, dict]]:
    """(probe kind, failure text, event) for each Unhealthy event."""
    out = []
    for e in v.events_where("Unhealthy"):
        m = _PROBE_EVENT.match(e["message"])
        if m:
            out.append((m.group(1).lower(), m.group(2), e))
    return out


def _probe_port(probe: dict) -> str | None:
    for key in ("http_get", "tcp_socket", "grpc"):
        if key in probe:
            m = re.search(r"port=(\S+)", probe[key])
            return m.group(1) if m else None
    return None


def _declared_ports(container: dict) -> dict[str, str]:
    """{'80': 'http=80/TCP', 'http': 'http=80/TCP'}: lookup by number and by name."""
    out = {}
    for p in container.get("ports", []):
        name, _, rest = p.rpartition("=")
        number = rest.split("/")[0]
        out[number] = p
        if name:
            out[name] = p
    return out


@rule
def probe_wrong_port(v: EvidenceView) -> RuleHit | None:
    for kind, text, event in _probe_failures(v):
        if "connection refused" not in text:
            continue
        probe = v.container.get("probes", {}).get(kind, {})
        port = _probe_port(probe)
        declared = _declared_ports(v.container)
        if port is None or not declared or port in declared:
            continue
        ports_text = ", ".join(sorted(set(declared.values())))
        target_port = next(iter(sorted(set(declared.values())))).split("=")[-1].split("/")[0]
        return _hit(
            "probe_wrong_port",
            FailureType.PROBE_FAILURE,
            finding=f"The {kind} probe checks port {port}, but the container declares only "
            f"{ports_text}.",
            cause=f"The {kind} probe points at port {port}, where nothing listens (the app "
            f"serves on {ports_text}). Every probe gets connection refused, so the kubelet "
            + (
                "restarts a container that may be perfectly healthy."
                if kind != "readiness"
                else "never marks the pod ready."
            ),
            fix=[
                f"Point the {kind} probe at port {target_port} (or the named port) instead "
                f"of {port}",
            ],
            certainty="high",
            evidence=[
                f"{kind} probe: {_probe_desc(probe)}",
                f"container ports: {ports_text}",
                f"event Unhealthy x{event.get('count', 1)}: {event['message']}",
            ],
        )
    return None


@rule
def probe_app_not_listening(v: EvidenceView) -> RuleHit | None:
    for kind, text, event in _probe_failures(v):
        if "connection refused" not in text:
            continue
        probe = v.container.get("probes", {}).get(kind, {})
        port = _probe_port(probe)
        declared = _declared_ports(v.container)
        if declared and port not in declared:
            continue  # probe_wrong_port covers it
        delay = probe.get("initial_delay_seconds", 0)
        has_startup = "startup" in v.container.get("probes", {})
        return _hit(
            "probe_app_not_listening",
            FailureType.PROBE_FAILURE,
            finding=f"Nothing accepts connections on the {kind} probe's port {port}.",
            cause=f"The app is not listening on port {port} when the {kind} probe runs: either "
            f"it starts slower than the probe allows (initialDelaySeconds={delay}"
            f"{', no startupProbe' if not has_startup else ''}) or it listens on a different "
            "port than declared.",
            fix=[
                "Check the logs for the port the app actually binds to",
                "If startup is just slow, add a startupProbe (or raise initialDelaySeconds)",
            ],
            certainty="medium",
            evidence=[
                f"{kind} probe: {_probe_desc(probe)}",
                f"event Unhealthy x{event.get('count', 1)}: {event['message']}",
            ],
        )
    return None


@rule
def probe_http_status(v: EvidenceView) -> RuleHit | None:
    for kind, text, event in _probe_failures(v):
        m = re.search(r"statuscode: (\d{3})", text)
        if not m:
            continue
        code = int(m.group(1))
        probe = v.container.get("probes", {}).get(kind, {})
        path = probe.get("http_get", "").split(" ")[1] if "http_get" in probe else "?"
        if code == 404:
            cause = f"The {kind} probe requests path {path}, which the app does not serve (404)."
            fix = [f"Point the probe at the app's real health endpoint instead of {path}"]
            certainty: Certainty = "high"
        elif code in (401, 403):
            cause = f"The app requires authentication on {path} ({code}); probes send none."
            fix = ["Expose an unauthenticated health endpoint and probe that"]
            certainty = "high"
        elif code >= 500:
            cause = (
                f"The app answers {code} on {path}: it reports itself unhealthy, often because "
                "a dependency (database, cache, downstream API) is unavailable."
            )
            fix = [
                "Read the app logs around the probe failures to see which check fails",
                "Keep liveness probes free of dependency checks so outages don't cause restarts",
            ]
            certainty = "medium"
        else:
            continue
        return _hit(
            "probe_http_status",
            FailureType.PROBE_FAILURE,
            finding=f"The {kind} probe gets HTTP {code} from {path}.",
            cause=cause,
            fix=fix,
            certainty=certainty,
            evidence=[
                f"{kind} probe: {_probe_desc(probe)}",
                f"event Unhealthy x{event.get('count', 1)}: {event['message']}",
            ],
        )
    return None


@rule
def probe_timeout(v: EvidenceView) -> RuleHit | None:
    for kind, text, event in _probe_failures(v):
        if not re.search(r"context deadline exceeded|Client\.Timeout|timed out|timeout", text):
            continue
        probe = v.container.get("probes", {}).get(kind, {})
        timeout = probe.get("timeout_seconds", 1)
        return _hit(
            "probe_timeout",
            FailureType.PROBE_FAILURE,
            finding=f"The {kind} probe times out after {timeout}s.",
            cause=f"The app does not answer the {kind} probe within timeoutSeconds={timeout}: "
            "it is overloaded, blocked (GC pauses, CPU throttling), or the health check itself "
            "is slow.",
            fix=[
                f"Raise timeoutSeconds above {timeout} if the endpoint is legitimately slow",
                "Check CPU throttling and limits; make the health endpoint cheap",
            ],
            certainty="medium",
            evidence=[
                f"{kind} probe: {_probe_desc(probe)}",
                f"event Unhealthy x{event.get('count', 1)}: {event['message']}",
            ],
        )
    return None


def _probe_desc(probe: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in probe.items())


# =============================================================================================
# CrashLoopBackOff
# =============================================================================================

# Upper-case identifiers that look like env var names, next to "not set"-style wording.
_ENV_PATTERNS = [
    re.compile(
        r"(?:env(?:ironment)?[ _-]?var(?:iable)?s?\s*:?\s*)['\"`]?([A-Z][A-Z0-9_]{2,})['\"`]?"
        r"\s+(?:is\s+)?(?:not set|not defined|undefined|missing|required|empty|unset)",
        re.I,
    ),
    re.compile(
        r"['\"`]?([A-Z][A-Z0-9_]{2,})['\"`]?\s+(?:environment variable|env var)\s+"
        r"(?:is\s+)?(?:not set|missing|required|undefined)",
        re.I,
    ),
    re.compile(
        r"missing (?:required )?(?:env(?:ironment)? var(?:iable)?s?)\s*:?\s*['\"`]?"
        r"([A-Z][A-Z0-9_]{2,})"
    ),
    re.compile(r"KeyError: ['\"]([A-Z][A-Z0-9_]{2,})['\"]"),  # Python os.environ["X"]
    # Bare "DATABASE_URL is not set". The underscore keeps words like "ERROR" from matching.
    re.compile(
        r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)['\"`]?\s+(?:is\s+)?"
        r"(?:not set|unset|not defined|undefined|missing|required)\b"
    ),
]


@rule
def crash_missing_env_var(v: EvidenceView) -> RuleHit | None:
    logs = v.logs
    if not logs or not v.container:
        return None
    for pattern in _ENV_PATTERNS:
        m = pattern.search(logs)
        if not m:
            continue
        name = m.group(1)
        if not name.isupper():
            continue
        line = _line_containing(logs, m.group(0))
        env_names = v.container.get("env_names", [])
        env_from = v.container.get("env_from", [])
        evidence = [f"log: {line}", f"env names on container: {', '.join(env_names) or 'none'}"]
        if env_from:
            evidence.append(f"envFrom: {', '.join(env_from)}")

        if name in env_names:
            cause = (
                f"The app reports {name} as missing, but the container does define {name}: its "
                "value is probably empty or comes from a ConfigMap/Secret key that is missing."
            )
            fix = [f"Check the value source of {name} (valueFrom key, ConfigMap/Secret contents)"]
            certainty: Certainty = "medium"
        elif env_from:
            cause = (
                f"The app exits because environment variable {name} is not set. It is not "
                f"defined directly on the container, and the envFrom sources "
                f"({', '.join(env_from)}) apparently do not provide it."
            )
            fix = [
                f"Add {name} to {env_from[0]}, or set it in the container's env",
                "Roll out again after the change",
            ]
            certainty = "medium"
        else:
            cause = (
                f"The app exits on startup because the required environment variable {name} "
                "is not set on the container."
            )
            fix = [
                f"Add {name} to the container's env (from a Secret if it holds credentials) "
                "and roll out again",
            ]
            certainty = "high"
        return _hit(
            "crash_missing_env_var",
            FailureType.CRASH_LOOP,
            finding=f"The app's logs say environment variable {name} is missing.",
            cause=cause,
            fix=fix,
            certainty=certainty,
            evidence=evidence,
        )
    return None


@rule
def crash_missing_config_object(v: EvidenceView) -> RuleHit | None:
    if v.waiting.get("reason") != "CreateContainerConfigError":
        return None
    message = v.waiting.get("message", "")
    missing_key = re.search(r"couldn't find key (\S+) in (ConfigMap|Secret) (\S+)", message)
    missing_obj = re.search(r'(configmap|secret) "([^"]+)" not found', message, re.I)
    if missing_key:
        key, kind, ref = missing_key.groups()
        cause = f"The container references key {key!r} in {kind} {ref}, which has no such key."
        fix = [f"Add key {key!r} to {kind} {ref}, or fix the key name in the pod spec"]
    elif missing_obj:
        kind, name = missing_obj.group(1).lower(), missing_obj.group(2)
        cause = f"The container's env references {kind} {name!r}, which does not exist."
        fix = [f"Create {kind} {name!r} in this namespace, or fix the name in the pod spec"]
    else:
        cause = f"The container's configuration cannot be built: {message}"
        fix = ["Check the env / envFrom / volume references named in the message"]
    return _hit(
        "crash_missing_config_object",
        FailureType.CRASH_LOOP,
        finding="Kubernetes cannot create the container: CreateContainerConfigError.",
        cause=cause,
        fix=fix,
        certainty="high" if (missing_key or missing_obj) else "medium",
        evidence=[f"container waiting: CreateContainerConfigError: {message}"],
    )


@rule
def crash_command_not_found(v: EvidenceView) -> RuleHit | None:
    term = v.termination()
    message = " ".join([v.waiting.get("message", ""), term.get("message", "")])
    not_found = re.search(
        r'exec: "[^"]+": (?:executable file not found|stat [^:]+: no such file or directory)',
        message,
    )
    if term.get("exit_code") != 127 and not not_found:
        return None
    command = " ".join((v.container.get("command") or []) + (v.container.get("args") or []))
    shown = f" ({command})" if command else " (the image's default entrypoint)"
    return _hit(
        "crash_command_not_found",
        FailureType.CRASH_LOOP,
        finding="The container's command cannot be found or executed (exit 127).",
        cause=f"The container command{shown} does not exist in the image: a typo, a wrong path, "
        "or a binary/shell the image does not include.",
        fix=[
            "Check the command and args in the pod spec against what the image contains",
            "For minimal images (distroless, scratch), there is no sh/bash; call the binary "
            "directly",
        ],
        certainty="high",
        evidence=[f"termination: exit {term.get('exit_code')}"]
        + ([f"runtime: {message.strip()}"] if message.strip() else [])
        + ([f"command: {command}"] if command else []),
    )


@rule
def crash_permission_denied(v: EvidenceView) -> RuleHit | None:
    term = v.termination()
    message = " ".join([v.waiting.get("message", ""), term.get("message", "")])
    if term.get("exit_code") != 126 and "permission denied" not in message:
        return None
    return _hit(
        "crash_permission_denied",
        FailureType.CRASH_LOOP,
        finding="The container's command is not executable (exit 126 / permission denied).",
        cause="The entrypoint exists but cannot be executed: missing execute bit, or the "
        "container's user (runAsUser / non-root) lacks permission.",
        fix=[
            "Make the file executable in the image (chmod +x in the Dockerfile)",
            "Check securityContext.runAsUser against the file's owner and permissions",
        ],
        certainty="high",
        evidence=[f"termination: exit {term.get('exit_code')}"],
    )


@rule
def crash_exits_successfully(v: EvidenceView) -> RuleHit | None:
    term = v.termination()
    if term.get("exit_code") != 0 or term.get("reason") != "Completed" or v.restarts == 0:
        return None
    if v.target.kind.value == "pod" and not v.target.owners:
        return None  # a bare pod may legitimately run to completion
    return _hit(
        "crash_exits_successfully",
        FailureType.CRASH_LOOP,
        finding="The container exits with code 0 and keeps being restarted.",
        cause="The main process finishes successfully instead of running in the foreground. A "
        "Deployment/StatefulSet expects a long-running process, so it restarts it and backs off.",
        fix=[
            "Make the main process stay in the foreground (no daemonizing, no `&`)",
            "If this is a one-off task, run it as a Job instead of a Deployment",
        ],
        certainty="medium",
        evidence=[f"termination: Completed, exit 0, {v.restarts} restarts"],
    )


@rule
def crash_signal(v: EvidenceView) -> RuleHit | None:
    term = v.termination()
    code = term.get("exit_code")
    if code not in (139, 134):
        return None
    what = "segmentation fault (SIGSEGV)" if code == 139 else "abort (SIGABRT)"
    return _hit(
        "crash_signal",
        FailureType.CRASH_LOOP,
        finding=f"The process died from a {what}, exit {code}.",
        cause=f"The application crashed with a {what}: a native-code bug, an incompatible "
        "native library, or a base image / CPU architecture mismatch.",
        fix=[
            "Check that the image is built for the node's architecture (amd64 vs arm64)",
            "Look for native library or base image changes in the last deploy",
        ],
        certainty="medium",
        evidence=[f"termination: {term.get('reason')}, exit {code}"],
    )


_ERROR_LINE = re.compile(r"\b(fatal|error|exception|panic|traceback|failed|cannot|refused)\b", re.I)


@rule
def crash_log_error(v: EvidenceView) -> RuleHit | None:
    """Fallback for crashes: point at the most relevant error line in the logs."""
    term = v.termination()
    code = term.get("exit_code")
    if code in (None, 0) or term.get("reason") == "OOMKilled" or code in KILLED_EXIT_CODES:
        return None
    lines = [ln for ln in v.logs.splitlines() if ln and not ln.startswith("===")]
    errors = [ln.strip() for ln in lines if _ERROR_LINE.search(ln)]
    if errors:
        return _hit(
            "crash_log_error",
            FailureType.CRASH_LOOP,
            finding=f"The container exits with code {code}; its logs report an error.",
            cause=f"The application fails at startup and exits with code {code}. Last error in "
            f'its logs: "{errors[-1][:300]}"',
            fix=[
                "Fix the error the log line names (configuration, dependency, or code)",
                f"Full logs: kubectl logs {v.target.pod} -c {v.container_name} --previous",
            ],
            certainty="low",
            evidence=[f"termination: exit {code}"] + [f"log: {e[:300]}" for e in errors[-3:]],
        )
    if not lines:
        return _hit(
            "crash_no_output",
            FailureType.CRASH_LOOP,
            finding=f"The container exits with code {code} without writing any logs.",
            cause="The process fails before it logs anything: a wrong command or arguments, a "
            "missing file it needs, or an app that logs to a file instead of stdout.",
            fix=[
                "Check the command and args, and the files the app expects at startup",
                "Run the image locally with the same command to reproduce",
            ],
            certainty="low",
            evidence=[f"termination: exit {code}", "logs: empty"],
        )
    return None


# =============================================================================================
# Workloads without pods
# =============================================================================================


@rule
def workload_failed_create(v: EvidenceView) -> RuleHit | None:
    events = v.events_where("FailedCreate")
    if not events:
        return None
    message = events[0]["message"]
    if "exceeded quota" in message:
        cause = "Pod creation is rejected because the namespace's ResourceQuota is exhausted."
        fix = [
            "Check usage: kubectl describe resourcequota -n <namespace>",
            "Lower the pod's requests, free quota, or ask for a larger quota",
        ]
        certainty: Certainty = "high"
    elif "forbidden" in message or "violates PodSecurity" in message:
        cause = "Pod creation is rejected by an admission policy (PodSecurity or a webhook)."
        fix = ["Adjust the pod's securityContext to satisfy the policy named in the message"]
        certainty = "high"
    else:
        cause = f"The controller cannot create pods: {message}"
        fix = ["Read the FailedCreate message and fix the object it names"]
        certainty = "medium"
    return _hit(
        "workload_failed_create",
        FailureType.UNKNOWN,
        finding="The workload's controller fails to create pods.",
        cause=cause,
        fix=fix,
        certainty=certainty,
        evidence=[f"event FailedCreate x{events[0].get('count', 1)}: {message}"],
    )


# --- helpers ------------------------------------------------------------------------------


def _hit(rule_id: str, failure_type: FailureType, **kwargs) -> RuleHit:
    return RuleHit(rule=rule_id, failure_type=failure_type, **kwargs)


def _line_containing(text: str, fragment: str) -> str:
    for line in text.splitlines():
        if fragment in line:
            return line.strip()
    return fragment


def _round_up_mi(n: float) -> float:
    """Round up to a tidy size: a multiple of 64Mi."""
    step = 64 * 2**20
    return -(-n // step) * step


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
