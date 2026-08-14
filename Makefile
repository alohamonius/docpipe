.PHONY: sync fmt lint typecheck test status infra-preview infra-up infra-down aurora-bootstrap

sync:
	uv sync --all-packages

fmt:
	uv run ruff format packages services scripts

lint:
	uv run ruff check packages services scripts

typecheck:
	uv run mypy packages/core/src

test:
	uv run pytest

# Live stack snapshot → status.json + dashboard.html (both gitignored).
status:
	uv run --with boto3 python scripts/status.py

# Infra targets operate on the dev stack (see pulumi/README.md for backend login).
#
# The self-managed S3 backend needs a passphrase even when it is empty, and every
# pulumi command fails without it. Empty is correct today: no secret values live
# in stack config, and the Aurora bedrock_user password is deliberately kept out
# of Pulumi state (scripts/aurora_bootstrap.py writes it straight to Secrets
# Manager). Set a real one before that stops being true:
#   PULUMI_CONFIG_PASSPHRASE=… make infra-up
PULUMI_CONFIG_PASSPHRASE ?=
export PULUMI_CONFIG_PASSPHRASE

infra-preview:
	cd pulumi && pulumi preview --diff

infra-up:
	cd pulumi && pulumi up

infra-down:
	cd pulumi && pulumi destroy

# The SQL half Pulumi cannot do. Between `enableAurora` and `enableAuroraKb`.
aurora-bootstrap:
	uv run --with boto3 python scripts/aurora_bootstrap.py
