"""Docker build logs: the parser, the build rules, `--build-log`, and the prompt.

The five lab cases (lab/cases/build-*) hold real BuildKit logs and are scored by
tests/test_lab_cases.py. These tests cover the shapes the lab does not: the legacy builder,
logs in no known format, colour codes, and each rule's other branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfa.ai import rules
from pfa.ai.context import build_prompt
from pfa.cli import app
from pfa.collectors.docker_build import BuildLogError, build_evidence, parse_build_log
from pfa.engine.orchestrator import analyze, load_build_log
from pfa.models import FailureType, WorkloadKind
from pfa.view import EvidenceView

from .fixtures.fake_secrets import OPAQUE

LAB = Path(__file__).resolve().parents[1] / "lab" / "cases"


def bk(*steps: str, final: str = "", extra: str = "") -> str:
    """A BuildKit --progress=plain log: steps are pre-formatted '#N ...' blocks."""
    head = '#0 building with "default" instance using docker driver\n\n'
    return head + "\n\n".join(steps) + "\n" + extra + (f"\nERROR: failed to build: {final}\n")


def view_of(log: str) -> EvidenceView:
    return EvidenceView(*build_evidence(Path("build.log"), log))


# --- parser -----------------------------------------------------------------------------------


def test_buildkit_failing_step_error_and_dockerfile_line():
    log = bk(
        "#4 [1/3] FROM docker.io/library/node:22-alpine@sha256:abc\n#4 DONE 0.1s",
        "#6 [3/3] RUN npm ci\n#6 1.20 npm ERR! code E404\n#6 1.21 npm ERR! 404 Not Found\n"
        '#6 ERROR: process "/bin/sh -c npm ci" did not complete successfully: exit code: 1',
        final='failed to solve: process "/bin/sh -c npm ci" did not complete successfully: '
        "exit code: 1",
        extra="Dockerfile:7\n--------------------\n   7 | >>> RUN npm ci\n--------------------",
    )
    summary, excerpt = parse_build_log(log)
    assert summary["builder"] == "buildkit"
    assert summary["failed_step"] == {
        "stage": "3/3",
        "instruction": "RUN npm ci",
        "dockerfile": "Dockerfile:7",
    }
    assert summary["exit_code"] == 1
    assert summary["base_images"] == ["node:22-alpine"]
    assert excerpt == "npm ERR! code E404\nnpm ERR! 404 Not Found"


def test_colour_codes_and_carriage_returns_are_stripped():
    log = bk(
        "#5 [2/2] COPY a.txt /a.txt\r\n#5 ERROR: \x1b[31mfailed to calculate checksum of ref x: "
        '"/a.txt": not found\x1b[0m',
        final="failed to solve: x",
        extra="\x1b[33m1 warning found:\x1b[0m\n"
        ' - CopyIgnoredFile: Attempting to Copy file "a.txt"',
    )
    summary, _ = parse_build_log(log)
    assert summary["error"] == 'failed to calculate checksum of ref x: "/a.txt": not found'
    assert summary["warnings"] == ['CopyIgnoredFile: Attempting to Copy file "a.txt"']


def test_legacy_builder_log():
    log = (
        "Sending build context to Docker daemon  3.072kB\n"
        "Step 1/4 : FROM python:3.12-slim\n ---> 1a2b3c\n"
        "Step 2/4 : RUN apt-get install -y libpq-dev\n ---> Running in 9f8e\n"
        "Reading package lists...\nE: Unable to locate package libpq-dev\n"
        "The command '/bin/sh -c apt-get install -y libpq-dev' returned a non-zero code: 100\n"
    )
    summary, excerpt = parse_build_log(log)
    assert summary["builder"] == "legacy"
    assert summary["failed_step"] == {
        "stage": "2/4",
        "instruction": "RUN apt-get install -y libpq-dev",
    }
    assert summary["exit_code"] == 100
    assert summary["base_images"] == ["python:3.12-slim"]
    assert "E: Unable to locate package libpq-dev" in excerpt


def test_unknown_format_keeps_the_text():
    summary, excerpt = parse_build_log("something went wrong\nerror: it broke\n")
    assert summary["builder"] == "unknown"
    assert excerpt.endswith("error: it broke")


def test_long_output_keeps_head_and_tail():
    lines = "\n".join(f"#6 0.{i:03d} line {i}" for i in range(200))
    log = bk(f"#6 [2/2] RUN make\n{lines}\n#6 ERROR: process failed: exit code: 2", final="x")
    _, excerpt = parse_build_log(log)
    kept = excerpt.splitlines()
    assert kept[0] == "line 0" and kept[-1] == "line 199"
    assert "... (145 lines omitted) ..." in kept


def test_pip_versions_are_read_before_long_lines_are_clipped():
    versions = ", ".join(f"2.{i}.0" for i in range(200)) + ", 3.0.0rc1"
    out = (
        "#8 0.8 ERROR: Could not find a version that satisfies the requirement foo==9.0 "
        f"(from versions: {versions})"
    )
    summary, excerpt = parse_build_log(bk(f"#8 [2/2] RUN pip install foo==9.0\n{out}", final="x"))
    assert summary["pip_unavailable"] == [
        {"requirement": "foo==9.0", "versions": 201, "newest": "2.199.0"}
    ]
    assert max(len(ln) for ln in excerpt.splitlines()) <= 400


def test_empty_log_is_refused():
    with pytest.raises(BuildLogError):
        build_evidence(None, "  \n")


# --- rules --------------------------------------------------------------------------------------


def _step(instruction: str, output: str, code: int = 1) -> str:
    out = "\n".join(f"#7 0.5 {ln}" for ln in output.splitlines())
    return bk(
        "#3 [1/2] FROM docker.io/library/python:3.12-slim\n#3 DONE 0.0s",
        f"#7 [2/2] {instruction}\n{out}\n#7 ERROR: process did not complete successfully: "
        f"exit code: {code}",
        final="x",
    )


def test_command_not_found():
    hit = rules.build_command_not_found(
        view_of(_step("RUN make build", "/bin/sh: 1: make: not found", 127))
    )
    assert hit.certainty == "high"
    assert "make, which is not installed" in hit.cause


def test_apt_without_update_in_the_same_step():
    v = view_of(
        _step("RUN apt-get install -y libpq-dev", "E: Unable to locate package libpq-dev", 100)
    )
    hit = rules.build_apt_package_missing(v)
    assert (hit.certainty, "apt-get update" in hit.fix[0]) == ("high", True)


def test_apt_with_update_means_the_name_is_wrong():
    v = view_of(
        _step(
            "RUN apt-get update && apt-get install -y libssl1.0",
            "E: Unable to locate package libssl1.0",
            100,
        )
    )
    hit = rules.build_apt_package_missing(v)
    assert hit.certainty == "medium"
    assert "no package named libssl1.0" in hit.cause


def test_private_base_image():
    log = bk(
        "#2 [internal] load metadata for registry.example.com/team/base:1.0\n"
        "#2 ERROR: pull access denied, repository does not exist or may require authorization",
        final="failed to solve: x",
        extra="Dockerfile:1\n--------------------\n"
        "   1 | >>> FROM registry.example.com/team/base:1.0",
    )
    hit = rules.build_base_image_missing(view_of(log))
    assert "refused access" in hit.cause
    assert any("Log the builder in" in f for f in hit.fix)


def test_copy_missing_without_a_dockerignore_warning_is_medium():
    log = bk(
        "#6 [3/3] COPY dist/ /app/\n"
        '#6 ERROR: failed to calculate checksum of ref x: "/dist": not found',
        final="failed to solve: x",
    )
    hit = rules.build_copy_missing_file(view_of(log))
    assert hit.certainty == "medium"
    assert "relative to the context directory" in hit.cause


def test_pip_with_no_versions_at_all_is_medium():
    out = (
        "ERROR: Could not find a version that satisfies the requirement internal-lib (from "
        "versions: none)\nERROR: No matching distribution found for internal-lib"
    )
    hit = rules.build_pip_version_missing(view_of(_step("RUN pip install internal-lib", out)))
    assert hit.certainty == "medium"
    assert "private index" in hit.cause


def test_source_build_with_a_pinned_base_image_does_not_blame_the_image():
    out = (
        "Collecting lxml==4.9.0\n  Downloading lxml-4.9.0.tar.gz (3.2 MB)\n"
        "  error: command '/usr/bin/gcc' failed: No such file or directory\n"
        '  File "/usr/local/lib/python3.12/site-packages/pip/x.py"\nFailed to build lxml'
    )
    hit = rules.build_dependency_source_build(view_of(_step("RUN pip install lxml==4.9.0", out)))
    assert "floating" not in hit.cause
    assert "lxml 4.9.0 has no prebuilt wheel for Python 3.12" in hit.cause
    assert any("build tools" in f for f in hit.fix)


def test_fallback_quotes_the_most_severe_line_and_skips_pip_notices():
    out = "fatal: could not read Username for 'https://github.com'\n[notice] A new release of pip"
    hit = rules.build_step_failed(view_of(_step("RUN git clone https://github.com/x/y", out, 128)))
    assert "fatal: could not read Username" in hit.cause
    assert hit.certainty == "low"


def test_build_rules_ignore_pod_evidence(make_view_crash):
    assert all(
        r(make_view_crash) is None or not r.__name__.startswith("build_") for r in rules.RULES
    )


@pytest.fixture
def make_view_crash():
    from .conftest import crashed, make_view

    return make_view(
        crashed(1), logs="E: Unable to locate package x\nNo matching distribution found for y"
    )


# --- end to end: classifier, prompt, redaction, CLI --------------------------------------------


def test_build_report_and_prompt():
    path = LAB / "build-floating-base-image" / "build.log"
    report = analyze(*load_build_log(path, path.read_text()))
    assert report.target.kind is WorkloadKind.BUILD
    assert report.classification.failure_type is FailureType.DOCKER_BUILD
    assert report.classification.problem.startswith("docker build fails at step 4/5")
    user = build_prompt(report).user
    assert "docker build log build.log (no cluster involved)" in user
    assert "Downloading numpy-1.24.4.tar.gz" in user
    assert "Pod and workload state" not in user


def test_secrets_in_build_output_are_redacted():
    log = _step("RUN curl -fsS https://api.example.com", f"> Authorization: Bearer {OPAQUE}\n401")
    target, evidence = load_build_log(None, log)
    assert OPAQUE not in (evidence[0].raw_excerpt or "")
    assert evidence[0].redacted


def test_cli_build_log_from_file_and_stdin():
    path = str(LAB / "build-base-image-typo" / "build.log")
    result = CliRunner().invoke(app, ["diagnose", "--build-log", path, "--no-llm", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert '"failure_type": "docker_build"' in result.stdout
    piped = CliRunner().invoke(
        app, ["diagnose", "--build-log", "-", "--no-llm"], input=Path(path).read_text()
    )
    assert piped.exit_code == 0 and "docker build log stdin" in piped.stdout
    collected = CliRunner().invoke(app, ["collect", "--build-log", path, "-o", "json"])
    assert '"source": "docker_build"' in collected.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["diagnose", "checkout-api", "--build-log", "x.log"],
        ["diagnose", "--build-log", "does-not-exist.log"],
        ["collect"],
    ],
)
def test_cli_build_log_misuse_exits_2(args):
    assert (
        CliRunner().invoke(app, [*args, "--no-llm"] if args[0] == "diagnose" else args).exit_code
        == 2
    )


def test_network_failure_names_the_later_errors_as_fallout():
    # Lab case build-download-dns: the step's last errors are tar's, on wget's empty output.
    v = view_of((LAB / "build-download-dns" / "build.log").read_text())
    hit = rules.build_network_unreachable(v)
    assert hit.cause.startswith("The build cannot resolve artifacts.pfa-lab.invalid")
    assert '("tar: short read") only follow from that' in hit.cause
    assert any("pipefail" in f for f in hit.fix)  # the RUN line pipes wget into tar
