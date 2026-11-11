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


def answer_from_rules(
    classification: Classification,
    hits: list[RuleHit],
    label: str = "",
    others: list[tuple[str, RuleHit]] | None = None,
) -> Diagnosis:
    """The --no-llm answer: the best rule hit, or an honest "unsure".

    `others` holds the best hit for each other failing container. Each one with a different
    cause is part of the answer: fixing only the first would leave the pod broken.
    """
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
    extra = [(lbl, h) for lbl, h in others or [] if h.cause != best.cause]
    cause, fix, certainty, evidence = best.cause, best.fix, best.certainty, best.evidence
    if extra and label:
        parts = [(label, best), *extra]
        cause = f"{len(parts)} containers fail, for different reasons. " + " ".join(
            f"{_upper_first(lbl)}: {h.cause}" for lbl, h in parts
        )
        fix = [f"{_upper_first(lbl)}: {f}" for lbl, h in parts for f in h.fix]
        certainty = max((h.certainty for _, h in parts), key=_CERTAINTY_ORDER.__getitem__)
        evidence = [e for _, h in parts for e in h.evidence]
    return Diagnosis(
        failure_type=failure_type,
        problem=classification.problem,
        likely_cause=cause,
        fix=fix,
        confidence=certainty,
        evidence_used=_dedupe(evidence + classification.signals),
        rule_hits=_dedupe([h.rule for h in hits] + [h.rule for _, h in extra]),
    )


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


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
            finding=f"{v.container_label.capitalize()} was OOMKilled but has no memory limit.",
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
        finding=f"{v.container_label.capitalize()} was OOMKilled {v.restarts} times; its memory "
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


# A registry, or a mirror/proxy in front of it, failing: `unexpected status from HEAD request to
# http://registry-mirror:1273/v2/billing/ui/manifests/3.2.0?ns=registry.example.com: 500 ...`
_SERVER_ERROR = re.compile(
    r"(?:request to (?P<url>\S+?): )?(?P<status>5\d\d [A-Z][A-Za-z ]+?)(?=\s*$|[,;\"\n])", re.M
)


@rule
def image_registry_unreachable(v: EvidenceView) -> RuleHit | None:
    messages = _pull_messages(v)
    m = re.search(
        r"no such host|i/o timeout|connection refused|TLS handshake timeout|"
        r"x509: certificate|context deadline exceeded",
        messages,
    )
    if m:
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
    m = _SERVER_ERROR.search(messages)
    if not m:
        return None
    status, url = m.group("status").strip(), m.group("url") or ""
    mirror = re.match(r"https?://([^/]+)/v2/", url)
    upstream = re.search(r"[?&]ns=([^&\s]+)", url)
    if mirror and upstream and mirror.group(1) != upstream.group(1):
        # The node pulled through a mirror, which could not get the image from the registry.
        cause = (
            f"Pulls go through the registry mirror {mirror.group(1)}, which answered "
            f'"{status}" for an image from registry {upstream.group(1)}: the mirror could not '
            "fetch it. Usually that registry hostname is wrong or unreachable from the mirror; "
            "otherwise the mirror itself is failing."
        )
        fix = [
            f"Check the registry hostname {upstream.group(1)} in the image reference",
            f"Check that the mirror {mirror.group(1)} can reach that registry (its logs and "
            "upstream settings)",
        ]
    else:
        cause = (
            f'The registry answered "{status}": it, or a proxy in front of it, is failing. '
            "Retrying may help if it is a temporary outage."
        )
        fix = [
            "Check the registry hostname in the image reference and the registry's status",
            "If a proxy or mirror sits in front of the registry, check its logs",
        ]
    return _hit(
        "image_registry_unreachable",
        FailureType.IMAGE_PULL,
        finding=f"Pulling the image failed with a server error ({status}).",
        cause=cause,
        fix=fix,
        certainty="medium",
        evidence=_pull_evidence(v, status),
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
    claims = v.summary("k8s_state").get("claims", [])
    names = v.pod.get("pvcs", []) if v.pod else []
    evidence = [f"scheduler: {message}"]
    for claim in claims:
        name = claim["name"]
        if claim.get("missing"):
            return _hit(
                "pending_pvc",
                FailureType.PENDING,
                finding=f"The pod's PersistentVolumeClaim {name} does not exist.",
                cause=f"The pod mounts PersistentVolumeClaim {name}, which does not exist in "
                f"namespace {v.target.namespace}.",
                fix=[f"Create the claim {name}, or fix claimName in the pod spec"],
                certainty="high",
                evidence=[*evidence, f"claim {name}: not found"],
            )
        failed = [e for e in claim.get("events", []) if e.get("reason") == "ProvisioningFailed"]
        sc = claim.get("storage_class")
        if failed and (
            m := re.search(r'storageclass[\w.]* "([^"]+)" not found', failed[0]["message"])
        ):
            return _hit(
                "pending_pvc",
                FailureType.PENDING,
                finding=f"Claim {name} asks for StorageClass {m.group(1)}, which does not exist.",
                cause=f"PersistentVolumeClaim {name} asks for storageClassName {m.group(1)}, "
                "which does not exist in the cluster, so no volume is provisioned and the pod "
                "cannot be scheduled.",
                fix=[
                    "List the StorageClasses that exist: kubectl get storageclass",
                    f"Recreate claim {name} with one of them (storageClassName cannot be "
                    f"changed on an existing claim), or create StorageClass {m.group(1)}",
                ],
                certainty="high",
                evidence=[
                    *evidence,
                    f"claim {name} event ProvisioningFailed: {failed[0]['message']}",
                ],
            )
        if failed:
            return _hit(
                "pending_pvc",
                FailureType.PENDING,
                finding=f"Provisioning a volume for claim {name} fails.",
                cause=f"PersistentVolumeClaim {name} (StorageClass {sc or 'default'}) cannot get "
                f"a volume: {failed[0]['message']}",
                fix=[
                    f"Fix what the provisioner reports: kubectl describe pvc {name}",
                    "Check the StorageClass's provisioner is running",
                ],
                certainty="medium",
                evidence=[
                    *evidence,
                    f"claim {name} event ProvisioningFailed: {failed[0]['message']}",
                ],
            )
    # Only the pod's side is visible: it waits for a claim, but not why the claim is stuck.
    pvcs = ", ".join(names) or "its PVC"
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
        certainty="medium",
        evidence=evidence
        + [f"claim {c['name']}: {c.get('phase') or c.get('error')}" for c in claims],
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
    """(probe kind, failure text, event): how each probe fails now.

    One per probe kind: the latest failure, ties to the most frequent. A probe often fails
    differently while the app starts (connection refused) than once it runs (HTTP 503); the
    early failure is history, not the diagnosis.
    """
    current: dict[str, tuple[str, str, dict]] = {}
    for e in v.events_where("Unhealthy"):
        m = _PROBE_EVENT.match(e["message"])
        if not m:
            continue
        kind = m.group(1).lower()
        rank = (e.get("last_seen") or "", e.get("count", 1))
        best = current.get(kind)
        if best is None or rank > (best[2].get("last_seen") or "", best[2].get("count", 1)):
            current[kind] = (kind, m.group(2), e)
    return list(current.values())


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
        elif code >= 500 and (failures := dns_failures(v.logs)):
            host, line = failures[-1]
            dns_cause, fix = _dns_advice(v, host)
            cause = (
                f"The app answers {code} on {path} because a dependency is missing, not because "
                f'the probe is wrong. Its logs: "{line}". {dns_cause}'
            )
            fix = [*fix, "Leave the probe as it is: it correctly keeps traffic away until then"]
            certainty = "medium"
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


# Error lines, most severe first. The cause of a crash is usually its most severe line, not its
# last one: apps log side effects (failed flushes, closed connections) while shutting down.
_SEVERITY = [
    re.compile(r"\b(fatal|panic|critical|crit|emerg)\b|traceback|unhandled exception", re.I),
    re.compile(r"\b(error|exception|failed|cannot|refused|err)\b", re.I),
]
# An env var name, as apps print it in an error about its value.
_ENV_NAME = re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+|[A-Z]{4,})\b")


def _error_lines(logs: str) -> list[str]:
    """Error lines of the most severe level present, in log order; [] if none."""
    lines = [ln.strip() for ln in logs.splitlines() if ln.strip() and not ln.startswith("===")]
    for level in _SEVERITY:
        found = [ln for ln in lines if level.search(ln)]
        if found:
            return _dedupe(found)
    return []


@rule
def crash_log_error(v: EvidenceView) -> RuleHit | None:
    """Fallback for crashes: point at the most severe error line in the logs."""
    term = v.termination()
    code = term.get("exit_code")
    if code in (None, 0) or term.get("reason") == "OOMKilled" or code in KILLED_EXIT_CODES:
        return None
    lines = [ln for ln in v.logs.splitlines() if ln and not ln.startswith("===")]
    errors = _error_lines(v.logs)
    if errors:
        line = errors[-1][:300]
        fix = [
            "Fix the error the log line names (configuration, dependency, or code)",
            f"Full logs: kubectl logs {v.target.pod} -c {v.container_name} --previous",
        ]
        certainty: Certainty = "low"
        # The line names an env var the container sets: its value is what the app rejects.
        env_names = set(v.container.get("env_names", []))
        named = [n for n in _ENV_NAME.findall(line) if n in env_names]
        cause = f'{_who_fails(v)} and exits with code {code}. Its most severe log line: "{line}"'
        if named:
            cause += f". It names {named[0]}, which the container sets: the app rejects its value."
            fix = [
                f"Check the value of {named[0]} in the container's env (or its ConfigMap/Secret) "
                "against what the log line says the app expects, and roll out again",
                *fix[1:],
            ]
            certainty = "medium"
        return _hit(
            "crash_log_error",
            FailureType.CRASH_LOOP,
            finding=f"The {v.container_label} exits with code {code}; its logs report an error.",
            cause=cause,
            fix=fix,
            certainty=certainty,
            evidence=[f"termination: exit {code}"] + [f"log: {e[:300]}" for e in errors[-3:]],
        )
    if not lines:
        return _hit(
            "crash_no_output",
            FailureType.CRASH_LOOP,
            finding=f"The {v.container_label} exits with code {code} without writing any logs.",
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


# How apps and libraries report a hostname that does not resolve: Go, glibc/Python, Node, Java,
# libpq, busybox. Group 1 is the host.
_DNS_FAILURE = [
    re.compile(r"lookup ([A-Za-z0-9][\w.-]*)(?: on [\d.:\[\]a-f]+)?: no such host"),
    re.compile(r"getaddrinfo (?:ENOTFOUND|EAI_AGAIN) ([A-Za-z0-9][\w.-]*)"),
    re.compile(r"UnknownHostException: ([A-Za-z0-9][\w.-]*)"),
    re.compile(r'could not translate host name "([^"]+)" to address'),
    re.compile(r"can't resolve '([^']+)'"),
    re.compile(r"bad address '([^']+)'"),  # busybox wget
    re.compile(r"Could not resolve host: ([A-Za-z0-9][\w.-]*)"),  # curl
    re.compile(r"Temporary failure resolving '([^']+)'"),  # apt
    re.compile(
        r"([A-Za-z0-9][\w.-]*)(?::\d+)?: (?:Name or service not known|nodename nor servname)"
    ),
]


def dns_failures(logs: str) -> list[tuple[str, str]]:
    """(host, log line) for each hostname the logs say does not resolve, first seen first."""
    found: dict[str, str] = {}
    for ln in logs.splitlines():
        for pattern in _DNS_FAILURE:
            if m := pattern.search(ln):
                found.setdefault(m.group(1).rstrip("."), ln.strip()[:300])
    return list(found.items())


def _dns_advice(v: EvidenceView, host: str) -> tuple[str, list[str]]:
    """What a failed lookup of `host` means, and what to check. A short name is a Service in
    the pod's namespace; pfa cannot read Services, so it says what to check, not what it saw."""
    ns = v.target.namespace
    short = host.split(".")[0]
    in_cluster = "." not in host or host.endswith((".svc", ".svc.cluster.local", ".cluster.local"))
    if in_cluster:
        cause = (
            f"The hostname {host} does not resolve: there is no Service named {short} in "
            f"namespace {ns} (or it is in another namespace and needs {short}.<namespace>)."
        )
        fix = [
            f"Check the Service exists: kubectl get svc {short} -n {ns}",
            f"Create the {short} Service (or deploy what it fronts), or point the app at the "
            "right Service name",
        ]
    else:
        cause = f"The hostname {host} does not resolve from inside the cluster."
        fix = [
            f"Check the name {host} for typos and that it exists in DNS",
            "Check the cluster's DNS can resolve external names (kubectl run a busybox pod and "
            f"nslookup {host})",
        ]
    return cause, fix


@rule
def crash_dns_lookup_failed(v: EvidenceView) -> RuleHit | None:
    term = v.termination()
    if term.get("exit_code") in (None, 0) or term.get("reason") == "OOMKilled":
        return None
    failures = dns_failures(v.logs)
    if not failures:
        return None
    host, line = failures[-1]
    cause, fix = _dns_advice(v, host)
    return _hit(
        "crash_dns_lookup_failed",
        FailureType.CRASH_LOOP,
        finding=f"The app cannot resolve {host} and exits.",
        cause=f"{_who_fails(v)}: it cannot reach a dependency. {cause}",
        fix=fix,
        # The lookup failure is certain; why (no Service, wrong name, DNS down) is not.
        certainty="medium",
        evidence=[f"log: {line}", f"termination: exit {term.get('exit_code')}"],
    )


_MISSING_FILE = re.compile(
    r"(?:open|stat|read|load(?:ing)?|reading)\s+['\"]?(/[\w./-]+)['\"]?\s*:\s*no such file or "
    r"directory|\[Errno 2\] No such file or directory: '(/[^']+)'|ENOENT[^']*'(/[^']+)'",
    re.I,
)


@rule
def crash_missing_file(v: EvidenceView) -> RuleHit | None:
    term = v.termination()
    if term.get("exit_code") in (None, 0) or term.get("reason") == "OOMKilled":
        return None
    m = _MISSING_FILE.search(v.logs)
    if not m:
        return None
    path = next(g for g in m.groups() if g)
    line = _line_containing(v.logs, m.group(0))
    mounts = v.container.get("mounts", [])
    covering = [mp for mp in mounts if path == mp or path.startswith(mp.rstrip("/") + "/")]
    directory = path.rsplit("/", 1)[0] or "/"
    if covering:
        cause = (
            f"it cannot open {path}. A volume is mounted at {covering[0]}, but it has no "
            f"{path.rsplit('/', 1)[-1]}: the ConfigMap/Secret key or file name is wrong."
        )
        fix = [f"Check the keys of the volume mounted at {covering[0]} match {path}"]
    else:
        cause = (
            f"it cannot open {path}: the file is not in the image and nothing is mounted at "
            f"{directory}."
        )
        fix = [
            f"Mount the file at {path} (e.g. a ConfigMap with key {path.rsplit('/', 1)[-1]} "
            f"mounted at {directory}), or fix the path the app is given",
        ]
    return _hit(
        "crash_missing_file",
        FailureType.CRASH_LOOP,
        finding=f"The app exits because {path} does not exist.",
        cause=f"{_who_fails(v)} because {cause}",
        fix=fix,
        certainty="medium",
        evidence=[f"log: {line}", f"mounts: {', '.join(mounts) or 'none'}"],
    )


# =============================================================================================
# Docker build failures (collectors/docker_build.py)
# =============================================================================================


def _build_step(v: EvidenceView) -> tuple[str, str]:
    """(instruction, where) of the failing step, e.g. ("RUN pip install ...", "Dockerfile:5")."""
    step = v.build.get("failed_step") or {}
    return step.get("instruction", ""), step.get("dockerfile", "the Dockerfile")


@rule
def build_base_image_missing(v: EvidenceView) -> RuleHit | None:
    instruction, where = _build_step(v)
    error = v.build.get("error", "")
    if not instruction.upper().startswith("FROM") or not re.search(
        r"not found|manifest unknown|failed to resolve source metadata|pull access denied|"
        r"unauthorized|denied",
        error,
    ):
        return None
    image = instruction.split()[1] if len(instruction.split()) > 1 else "the base image"
    private = re.search(r"pull access denied|unauthorized|denied", error)
    cause = (
        f"The base image {image} cannot be pulled: the registry refused access (a private "
        "repository needs `docker login` on the builder, or the name is wrong)."
        if private
        else f"The base image {image} does not exist: the registry has no such tag (or "
        "repository), so the build stops before its first step."
    )
    return _hit(
        "build_base_image_missing",
        FailureType.DOCKER_BUILD,
        finding=f"The base image {image} cannot be resolved.",
        cause=cause,
        fix=[
            f"Correct the FROM line at {where}: check the tag exists (docker pull {image}, or "
            "the image's tag list on its registry)",
        ]
        + (["Log the builder in to the registry, or use a public image"] if private else []),
        certainty="high",
        evidence=[f"build error: {error}", f"failing line: {instruction}"],
    )


_COPY_MISSING = re.compile(
    r'"/?([^"]+)": not found|file not found in build context[^:]*: stat ([^:]+):|'
    r"lstat [^:]*?/([^/:]+): no such file or directory"
)


@rule
def build_copy_missing_file(v: EvidenceView) -> RuleHit | None:
    instruction, where = _build_step(v)
    error = v.build.get("error", "")
    if not re.match(r"(COPY|ADD)\b", instruction, re.I):
        return None
    m = _COPY_MISSING.search(error)
    if not m:
        return None
    path = next(g for g in m.groups() if g)
    ignored = [w for w in v.build.get("warnings", []) if w.startswith("CopyIgnoredFile")]
    if ignored:
        return _hit(
            "build_copy_missing_file",
            FailureType.DOCKER_BUILD,
            finding=f"{path} is excluded by .dockerignore.",
            cause=f"{instruction.split()[0]} {path} fails because .dockerignore excludes it, so "
            "the file is not in the build context even though it exists on disk.",
            fix=[
                f"Change .dockerignore so it no longer excludes {path} (or the directory it is "
                "in), or stop copying it",
            ],
            certainty="high",
            evidence=[f"build error: {error}", f"build warning: {ignored[0]}"],
        )
    return _hit(
        "build_copy_missing_file",
        FailureType.DOCKER_BUILD,
        finding=f"{path} is not in the build context.",
        cause=f"{instruction.split()[0]} {path} fails because the file is not in the build "
        "context: COPY paths are relative to the context directory given to docker build (not "
        "to the Dockerfile), and .dockerignore may exclude it.",
        fix=[
            f"Check {path} exists under the build context directory, and build from the right "
            "directory (docker build -f path/Dockerfile <context>)",
            "Check .dockerignore does not exclude it",
        ],
        certainty="medium",
        evidence=[f"build error: {error}", f"failing line: {instruction} ({where})"],
    )


_PIP_NO_MATCH = re.compile(r"No matching distribution found for (\S+)")


@rule
def build_pip_version_missing(v: EvidenceView) -> RuleHit | None:
    out = v.build_output
    m = _PIP_NO_MATCH.search(out)
    if not m:
        return None
    req = m.group(1)
    package = re.split(r"[=<>!~\[;]", req, maxsplit=1)[0]
    seen = next((u for u in v.build.get("pip_unavailable", []) if u["requirement"] == req), {})
    if seen.get("newest"):
        cause = (
            f"requirements pin {req}, a version that does not exist on the package index "
            f"(it lists {seen['versions']} versions of {package}; the newest release is "
            f"{seen['newest']})."
        )
        fix = [
            f"Change {req} in requirements.txt to an existing version, e.g. "
            f"{package}=={seen['newest']}",
        ]
        certainty: Certainty = "high"
    else:
        python = v.build.get("python")
        cause = (
            f"pip finds no installable version of {package} at all: the name is wrong, the "
            "package is on a private index the build cannot reach, or it has no release for "
            f"this Python{f' ({python})' if python else ''} or platform."
        )
        fix = [
            f"Check the name {package} in requirements.txt",
            "If it is on a private index, pass --index-url (via a build secret, not an ARG)",
        ]
        certainty = "medium"
    return _hit(
        "build_pip_version_missing",
        FailureType.DOCKER_BUILD,
        finding=f"pip cannot find {req}.",
        cause=cause,
        fix=fix,
        certainty=certainty,
        evidence=[f"build output: {_line_containing(out, m.group(0))}"],
    )


_SOURCE_BUILD_FAILED = re.compile(
    r"BackendUnavailable|subprocess-exited-with-error|Failed building wheel|Failed to build|"
    r"metadata-generation-failed|Getting requirements to build wheel did not run successfully|"
    r"error: command '[^']*(?:gcc|cc|g\+\+)' failed",
)
# A tag that moves: none, "latest", or only a major version ("3", "3-slim", "22-alpine").
_FLOATING_TAG = re.compile(r"^(latest|\d+(-[a-z]+)?)$")


@rule
def build_dependency_source_build(v: EvidenceView) -> RuleHit | None:
    """pip fell back to a source download, and building it failed."""
    out = v.build_output
    downloads = v.build.get("source_downloads", [])
    m = _SOURCE_BUILD_FAILED.search(out)
    if not downloads or not m:
        return None
    archive = downloads[0]
    package, _, version = re.sub(r"\.(tar\.gz|zip|tar\.bz2)$", "", archive).rpartition("-")
    python = v.build.get("python")
    on = f"Python {python}" if python else "this Python"
    images = v.build.get("base_images", [])
    floating = [
        i
        for i in images
        if ":" not in i.rsplit("/", 1)[-1] or _FLOATING_TAG.match(i.rsplit(":", 1)[1])
    ]
    cause = (
        f"{package} {version} has no prebuilt wheel for {on}, so pip downloads the source "
        f"({archive}) and building it fails."
    )
    fix = [f"Upgrade {package} to a release that ships wheels for {on}"]
    if floating:
        cause += (
            f" The base image {floating[0]} is a floating tag that now gives {on}: nothing in "
            "the Dockerfile changed, the base image did."
        )
        fix = [
            f"Pin the base image to the Python version {package} {version} supports (e.g. "
            f"python:3.11-slim) instead of the floating tag {floating[0]}",
            *fix,
        ]
    else:
        fix.append("Or install the build tools it needs (compiler, headers) before pip install")
    return _hit(
        "build_dependency_source_build",
        FailureType.DOCKER_BUILD,
        finding=f"Building {package} {version} from source fails.",
        cause=cause,
        fix=fix,
        # The fallback to source and the failure are certain; the build's own error is not
        # interpreted, so which part of the source build broke is not claimed.
        certainty="medium",
        evidence=[
            f"build output: Downloading {archive}",
            # The last matching line: in a traceback, the first is an outer frame ("raise X(").
            f"build output: {_last_line_containing(out, _SOURCE_BUILD_FAILED)}",
        ]
        + ([f"base image: {floating[0]} (floating tag)"] if floating else []),
    )


@rule
def build_network_unreachable(v: EvidenceView) -> RuleHit | None:
    out = v.build_output
    failures = dns_failures(out)
    if not failures:
        return None
    host, line = failures[0]
    instruction, where = _build_step(v)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # What the step printed after the failed lookup is fallout (tar on empty input, and so on).
    after = lines[lines.index(line) + 1 :] if line in lines else []
    cause = (
        f"The build cannot resolve {host}, so the download in this step gets nothing. Builds "
        "use the builder's network and DNS, not your shell's (VPN, proxy and internal DNS often "
        "do not apply)."
    )
    if after:
        cause += f' The errors after it ("{after[-1][:120]}") only follow from that.'
    fix = [
        f"Make {host} resolvable and reachable from the build (proxy build args, "
        "docker build --network, or the builder's DNS)",
        "Or put the file in the build context and COPY it instead of downloading it",
    ]
    if "|" in instruction:
        fix.append("Add set -o pipefail to piped RUN steps, so the first failure fails the step")
    return _hit(
        "build_network_unreachable",
        FailureType.DOCKER_BUILD,
        finding=f"The build cannot resolve {host}.",
        cause=cause,
        fix=fix,
        certainty="medium",
        evidence=[f"build output: {line}", f"failing line: {instruction} ({where})"],
    )


@rule
def build_command_not_found(v: EvidenceView) -> RuleHit | None:
    out = v.build_output
    m = re.search(
        r"(?:/bin/(?:ba)?sh: (?:\d+: )?(?:line \d+: )?)([\w./-]+): (?:command )?not found", out
    )
    if not m or v.build.get("exit_code") not in (127, None):
        return None
    command = m.group(1)
    images = ", ".join(v.build.get("base_images", [])) or "the base image"
    return _hit(
        "build_command_not_found",
        FailureType.DOCKER_BUILD,
        finding=f"{command} is not installed in the image at this step.",
        cause=f"The step runs {command}, which is not installed in the image at that point "
        f"({images}); slim and alpine images leave most tools out.",
        fix=[
            f"Install {command} in an earlier RUN step (apt-get install / apk add), or use a "
            "base image that has it",
            "In a multi-stage build, check the step runs in the stage that installed it",
        ],
        certainty="high",
        evidence=[f"build output: {_line_containing(out, m.group(0))}"],
    )


@rule
def build_apt_package_missing(v: EvidenceView) -> RuleHit | None:
    out = v.build_output
    m = re.search(r"E: (?:Unable to locate package |Package '?)([\w.+-]+)", out)
    if not m:
        return None
    package = m.group(1)
    instruction, _ = _build_step(v)
    if "apt-get update" not in instruction and "apt update" not in instruction:
        cause = (
            f"apt cannot find {package} because this RUN step installs without running "
            "apt-get update first; base images ship without package lists."
        )
        fix = ["Run apt-get update && apt-get install -y ... in the same RUN step"]
        certainty: Certainty = "high"
    else:
        cause = (
            f"apt has no package named {package} for this distribution release: the name is "
            "wrong or it was renamed in the base image's Debian/Ubuntu version."
        )
        fix = [f"Check the package name for this release (apt-cache search {package})"]
        certainty = "medium"
    return _hit(
        "build_apt_package_missing",
        FailureType.DOCKER_BUILD,
        finding=f"apt cannot locate {package}.",
        cause=cause,
        fix=fix,
        certainty=certainty,
        evidence=[f"build output: {_line_containing(out, m.group(0))}"],
    )


@rule
def build_step_failed(v: EvidenceView) -> RuleHit | None:
    """Fallback: quote the failing step's most severe output line."""
    if not v.build:
        return None
    instruction, where = _build_step(v)
    noise = re.compile(r"^\[notice\]|Ignored the following yanked versions")
    errors = [ln for ln in _error_lines(v.build_output) if not noise.search(ln)]
    line = errors[-1][:300] if errors else v.build.get("error", "no error line in the log")
    return _hit(
        "build_step_failed",
        FailureType.DOCKER_BUILD,
        finding=f"The build step {instruction or '?'} fails.",
        cause=f"The step {instruction or '?'} ({where}) fails. Its most severe output line: "
        f'"{line}"',
        fix=[
            "Fix what that line names, then rebuild",
            "Rebuild with --progress=plain --no-cache to see the step's full output",
        ],
        certainty="low",
        evidence=[f"build output: {line}"],
    )


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


def _who_fails(v: EvidenceView) -> str:
    if v.is_init_container:
        return (
            f"Init container {v.container_name} fails, so the app containers never start. It "
            "fails at startup"
        )
    return "The application fails at startup"


def _hit(rule_id: str, failure_type: FailureType, **kwargs) -> RuleHit:
    return RuleHit(rule=rule_id, failure_type=failure_type, **kwargs)


def _last_line_containing(text: str, pattern: re.Pattern[str]) -> str:
    return next((ln.strip()[:300] for ln in reversed(text.splitlines()) if pattern.search(ln)), "")


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
