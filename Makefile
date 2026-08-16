.PHONY: sync fmt lint typecheck test status kb-eval kb-live-check infra-preview infra-up infra-down aurora-bootstrap

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

# Assert the DEPLOYED data source against the chunking contract — the half
# `test_kb_chunking_contract.py` cannot reach, because it reads Pulumi source
# and a console edit does not touch source. Read-only: one `get_data_source`
# call, no writes, no spend. Ids resolve from the stack, so this works right
# after `make infra-up`; override either variable to point at another stack.
#
# NOT in `make test`, on purpose: `conftest.py` poisons AWS credentials for the
# whole unit suite so nothing can reach a real account by accident. The guard
# against that becoming a loophole is `test_the_live_check_is_reachable`, which
# DOES run in `make test` and fails if this target or its variables disappear.
AWS_PROFILE ?= docpipe
LIVE_KB_ID ?= $(shell PULUMI_CONFIG_PASSPHRASE= pulumi -C pulumi stack output knowledge_base_id 2>/dev/null)
LIVE_DS_ID ?= $(shell PULUMI_CONFIG_PASSPHRASE= pulumi -C pulumi stack output kb_data_source_id 2>/dev/null)
kb-live-check:
	DOCPIPE_LIVE_STACK=1 AWS_PROFILE=$(AWS_PROFILE) \
	KNOWLEDGE_BASE_ID=$(LIVE_KB_ID) KB_DATA_SOURCE_ID=$(LIVE_DS_ID) \
		uv run --with boto3 pytest packages/core/tests/test_kb_data_source_live.py -v

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
