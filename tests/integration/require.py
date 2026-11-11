"""Skip-or-fail for the integration tests (see `unavailable`)."""

from __future__ import annotations

import os

import pytest


def unavailable(reason: str) -> None:
    """Skip when a cluster, the lab or a tool is missing; fail instead in CI (REQUIRE_CLUSTER=1).

    Locally a skip is the right answer to "no cluster here". In CI it would turn a broken setup
    into a green run that tested nothing.
    """
    if os.environ.get("REQUIRE_CLUSTER") == "1":
        pytest.fail(f"REQUIRE_CLUSTER=1 but {reason}")
    pytest.skip(reason)
