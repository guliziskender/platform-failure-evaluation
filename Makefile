# Developer shortcuts. The lab targets your current kube context (local clusters only).
.PHONY: lab-up lab-down lab-status lab-reset lab-snapshot test test-integration test-llm test-helm eval eval-live image lint

lab-up:
	uv run python lab/lab.py up

lab-down:
	uv run python lab/lab.py down

lab-status:
	uv run python lab/lab.py status

lab-reset:
	uv run python lab/lab.py reset

lab-snapshot:
	uv run python lab/lab.py snapshot

test:
	uv run pytest

test-integration:
	uv run pytest -m "integration and not llm and not helm"

# Sends (fake) lab evidence to the configured AI provider. Needs PFA_PROVIDER etc.
test-llm:
	uv run pytest -m llm -v

# Score the rules against the saved evidence. For a model: uv run pfa eval --provider ...
eval:
	uv run pfa eval --no-llm

# Same, on evidence read from the running lab. Leaves the saved snapshots alone.
eval-live: lab-up
	uv run pfa eval --no-llm --live

# Build the image and install the chart into the local cluster, then check it from inside.
test-helm:
	uv run pytest -m helm -v

# Build pfa:dev and load it into the current local cluster (kind, Docker Desktop, minikube).
image:
	deploy/load-image.sh

lint:
	uv run ruff check . && uv run ruff format --check .
