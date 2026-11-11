"""Docker build logs: find the failing step, its error and its output.

Reads BuildKit `--progress=plain` output (the default for `docker build` in CI) and the legacy
builder's "Step 3/5 : ..." output. Facts only; the rules interpret them. No cluster involved.

BuildKit numbers each step (#5) and prefixes its output with the step number and a timestamp.
The summary box after a failure repeats only the step's last lines, so the step's own output is
read instead: in a long pip traceback, the line that names the cause is often near the top.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pfa.collectors.base import drop_none
from pfa.models import Evidence, Target, WorkloadKind

SOURCE = "docker_build"
HEAD_LINES = 15  # of the failing step's output: what it was doing (Collecting, Downloading)
TAIL_LINES = 40  # and how it ended
MAX_LINE = 400  # pip prints every available version on one line

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BK_HEADER = re.compile(r"^#(\d+) \[(.+?)\] (.*)$")  # #5 [2/4] RUN pip install ...
_BK_OUTPUT = re.compile(r"^#(\d+) \d+\.\d+ ?(.*)$")  # #5 0.123 some output
_BK_ERROR = re.compile(r"^#(\d+) ERROR: (.*)$")
_BK_FINAL = re.compile(r"^ERROR: (?:failed to build: )?(failed to solve: .*)$")
_EXIT_CODE = re.compile(r"(?:exit code|non-zero code): (\d+)")
_DOCKERFILE_POS = re.compile(r"^(\S*Dockerfile\S*):(\d+)$")
_POINTED = re.compile(r"^\s*(\d+) \| >>> ?(.*)$")
_WARNING = re.compile(r"^\s*- (\w+): (.*)$")  # BuildKit lint: " - CopyIgnoredFile: ..."
_LEGACY_STEP = re.compile(r"^Step (\d+/\d+) : (.*)$")
_LEGACY_ERROR = re.compile(
    r"^(The command .* returned a non-zero code: \d+|(?:COPY|ADD) failed: .*|ERROR: .*|"
    r"failed to .*|manifest for .* not found.*|pull access denied.*)$"
)
_PROGRESS = re.compile(r"^\s*[━─╸╺|#=\-> ]*\d+(\.\d+)?/\d+(\.\d+)? [kMG]?B\b")
_PYTHON = re.compile(r"/python(3\.\d+)/")
# pip lists every version on one line; read it before lines are clipped.
_PIP_UNAVAILABLE = re.compile(
    r"Could not find a version that satisfies the requirement (\S+) \(from versions: ([^)]*)\)"
)
_SOURCE_DOWNLOAD = re.compile(r"Downloading (\S+?\.(?:tar\.gz|zip|tar\.bz2))\b")
_FROM_STEP = re.compile(r"^FROM (\S+?)(?:@sha256:\w+)?(?: AS \S+)?$", re.I)
_METADATA = re.compile(r"^load metadata for (\S+)$")


class BuildLogError(ValueError):
    pass


def build_evidence(path: Path | None, text: str) -> tuple[Target, list[Evidence]]:
    """A Target and Evidence for a build log (path None = read from stdin)."""
    if not text.strip():
        raise BuildLogError("the build log is empty")
    name = path.name if path else "stdin"
    target = Target(namespace="", name=name, kind=WorkloadKind.BUILD)
    summary, excerpt = parse_build_log(text)
    return target, [Evidence(source=SOURCE, summary=summary, raw_excerpt=excerpt or None)]


def parse_build_log(text: str) -> tuple[dict[str, Any], str]:
    """(summary, excerpt of the failing step's output)."""
    lines = [_ANSI.sub("", ln).rstrip("\r") for ln in text.splitlines()]
    if any(_BK_HEADER.match(ln) for ln in lines):
        summary, output = _buildkit(lines)
    elif any(_LEGACY_STEP.match(ln) for ln in lines):
        summary, output = _legacy(lines)
    else:
        summary, output = {"builder": "unknown"}, [ln for ln in lines if ln.strip()]
    output = [ln for ln in output if not _PROGRESS.match(ln)]
    joined = "\n".join(output)
    summary.update(
        drop_none(
            {
                "python": _last(_PYTHON.findall(joined)),
                "source_downloads": _dedupe(_SOURCE_DOWNLOAD.findall(joined)) or None,
                "pip_unavailable": [
                    _pip_unavailable(req, versions)
                    for req, versions in _PIP_UNAVAILABLE.findall(joined)
                ]
                or None,
                "output_lines": len(output),
            }
        )
    )
    return drop_none(summary), _excerpt(output)


def _buildkit(lines: list[str]) -> tuple[dict[str, Any], list[str]]:
    steps: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    final_error = dockerfile = pointed = None
    warnings: list[str] = []
    for ln in lines:
        if m := _BK_HEADER.match(ln):
            num = m.group(1)
            if num not in steps:
                steps[num] = {"stage": m.group(2), "instruction": m.group(3), "output": []}
                order.append(num)
        elif m := _BK_ERROR.match(ln):
            steps.setdefault(m.group(1), {"stage": "?", "instruction": "?", "output": []})
            steps[m.group(1)]["error"] = m.group(2)
        elif m := _BK_OUTPUT.match(ln):
            if m.group(1) in steps:
                steps[m.group(1)]["output"].append(m.group(2))
        elif m := _BK_FINAL.match(ln):
            final_error = m.group(1)
        elif m := _DOCKERFILE_POS.match(ln):
            dockerfile = f"{m.group(1)}:{m.group(2)}"
        elif (m := _POINTED.match(ln)) and pointed is None:
            pointed = m.group(2).strip()
        elif m := _WARNING.match(ln):
            warnings.append(f"{m.group(1)}: {m.group(2)}")

    failed = next((steps[n] for n in order if "error" in steps[n]), None)
    if failed is None:
        failed = next((s for s in steps.values() if "error" in s), None)
    if failed is None:
        # A log cut off before the step's ERROR line: the last step that printed anything.
        failed = next((steps[n] for n in reversed(order) if steps[n]["output"]), None)
    base_images = []
    for s in steps.values():
        instr = s["instruction"]
        if (m := _FROM_STEP.match(instr)) or (m := _METADATA.match(instr)):
            base_images.append(_short_image(m.group(1)))
    error = (failed or {}).get("error") or final_error
    summary = {
        "builder": "buildkit",
        "failed_step": drop_none(
            {
                "stage": failed["stage"],
                "instruction": pointed or failed["instruction"],
                "dockerfile": dockerfile,
            }
        )
        if failed
        else None,
        "error": _clip(error),
        "exit_code": _exit_code(error or final_error or ""),
        "warnings": [_clip(w) for w in warnings] or None,
        "base_images": _dedupe(base_images) or None,
    }
    return summary, (failed or {}).get("output", [])


def _legacy(lines: list[str]) -> tuple[dict[str, Any], list[str]]:
    """The classic builder: steps in order; the last one started is the one that failed."""
    step = instruction = None
    output: list[str] = []
    base_images = []
    error = None
    for ln in lines:
        if m := _LEGACY_STEP.match(ln):
            step, instruction, output = m.group(1), m.group(2), []
            if m2 := _FROM_STEP.match(instruction):
                base_images.append(_short_image(m2.group(1)))
        elif _LEGACY_ERROR.match(ln.strip()):
            error = ln.strip()
        elif step and not ln.startswith((" ---> ", "Removing intermediate", "Successfully")):
            output.append(ln)
    summary = {
        "builder": "legacy",
        "failed_step": {"stage": step, "instruction": instruction} if step else None,
        "error": _clip(error),
        "exit_code": _exit_code(error or ""),
        "base_images": _dedupe(base_images) or None,
    }
    return summary, output


def _pip_unavailable(requirement: str, versions: str) -> dict[str, Any]:
    """The requirement pip could not satisfy, and the newest final release it did see."""
    listed = [v.strip() for v in versions.split(",") if v.strip() and v.strip() != "none"]
    released = [v for v in listed if re.fullmatch(r"[\d.]+", v)]
    return drop_none(
        {"requirement": requirement, "versions": len(listed), "newest": _last(released)}
    )


def _excerpt(output: list[str]) -> str:
    output = [_clip(ln) for ln in output]
    if len(output) <= HEAD_LINES + TAIL_LINES:
        return "\n".join(output)
    skipped = len(output) - HEAD_LINES - TAIL_LINES
    return "\n".join(
        [*output[:HEAD_LINES], f"... ({skipped} lines omitted) ...", *output[-TAIL_LINES:]]
    )


def _short_image(ref: str) -> str:
    return ref.removeprefix("docker.io/library/").removeprefix("docker.io/")


def _exit_code(text: str) -> int | None:
    m = _EXIT_CODE.search(text)
    return int(m.group(1)) if m else None


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 3] + "..."


def _last(items: list[str]) -> str | None:
    return items[-1] if items else None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
