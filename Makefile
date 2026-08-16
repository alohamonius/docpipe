.PHONY: sync fmt lint typecheck test status kb-eval infra-preview infra-up infra-down aurora-bootstrap

sync:
	uv sync --all-packages

fmt:
	uv run ruff format packages services scripts

# Both halves, because CI runs both. `ruff check` alone went green locally while
# CI failed on `format --check` for two consecutive runs — a gate you can pass
# locally and fail remotely is a gate people learn to ignore.
lint:
	uv run ruff check packages services scripts
	uv run ruff format --check packages services scripts

typecheck:
	uv run mypy packages/core/src

test:
	uv run pytest

# Live stack snapshot → status.json + dashboard.html (both gitignored).
status:
	uv run --with boto3 python scripts/status.py

# Score the live KB against health.studio's held-out question set — the number
# that makes 04-embedding-payload and 05-chunk-shape report a signed delta
# instead of an opinion. One retrieval call per question (63 today), so it
# costs real money and needs a live index: nothing has been ingested yet.
# Refuses an unratified answer key; pass DRY=--dry-run to rehearse the harness.
QUESTIONS ?= ../health.studio/apps/web/src/lib/kb/eval/questions.json
KB_ID ?=
OUT ?= .scratch/kb-eval.json
DRY ?=
kb-eval:
	uv run --with boto3 python scripts/kb_eval.py \
		--questions $(QUESTIONS) --kb-id $(KB_ID) --out $(OUT) $(DRY)

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
