.PHONY: sync fmt lint typecheck test status kb-eval kb-agentic-eval kb-live-check lambda-zip infra-preview infra-up infra-down aurora-bootstrap playground graph-index

sync:
	uv sync --all-packages

# PULUMI IS IN THE LINT SET, and it has to be spelled out here because CI splits
# it into a separate job (.github/workflows/ci.yml: `python` lints packages /
# services / scripts, `infra` lints infra/components + infra/__main__.py).
PY_SRC ?= packages services scripts
INFRA_SRC ?= infra/components infra/__main__.py

fmt:
	uv run ruff format $(PY_SRC) $(INFRA_SRC)

# Both halves of ruff AND both halves of the tree, because CI runs all four.
# `ruff check` alone went green locally while CI failed on `format --check` for
# two consecutive runs — a gate you can pass locally and fail remotely is a gate
# people learn to ignore. The same hole reopened on the pulumi side and cost a
# red PR: `lint` covered only $(PY_SRC), so a 107-char line added to
# infra/components/kb.py by the rerank IAM grant passed every local gate and
# failed CI's `infra` job. A second job nobody can run locally is the same bug as
# a second command nobody runs locally.
#
# One residual divergence, named rather than papered over: CI's infra job invokes
# `uvx ruff` (unpinned, latest) where everything here uses `uv run ruff` (the
# locked dev dep). A new ruff release can therefore still fail CI on a tree this
# target calls clean. Aligning them is a repo-owner call, not a lint fix.
lint:
	uv run ruff check $(PY_SRC) $(INFRA_SRC)
	uv run ruff format --check $(PY_SRC) $(INFRA_SRC)

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
# costs real money. Both KBs have held the 496-chunk corpus since 2026-08-20,
# so this runs for real — point KB_ID at JDNNGSU1JT (S3 Vectors) or A44CISMRAM
# (Aurora); the eval path is store-agnostic.
# Refuses an unratified answer key; pass DRY=--dry-run to rehearse the harness.
QUESTIONS ?= ../health.studio/apps/web/src/lib/kb/eval/questions.json
KB_ID ?=
OUT ?= .scratch/kb-eval.json
DRY ?=
RERANK ?=
SEARCH ?=
FLAGS ?=
kb-eval:
	uv run --with boto3 python scripts/kb_eval.py \
		--questions $(QUESTIONS) --kb-id $(KB_ID) --out $(OUT) $(DRY) $(RERANK) $(SEARCH) $(FLAGS)

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
LIVE_KB_ID ?= $(shell PULUMI_CONFIG_PASSPHRASE= pulumi -C infra stack output knowledge_base_id 2>/dev/null)
LIVE_DS_ID ?= $(shell PULUMI_CONFIG_PASSPHRASE= pulumi -C infra stack output kb_data_source_id 2>/dev/null)
kb-live-check:
	DOCPIPE_LIVE_STACK=1 AWS_PROFILE=$(AWS_PROFILE) \
	KNOWLEDGE_BASE_ID=$(LIVE_KB_ID) KB_DATA_SOURCE_ID=$(LIVE_DS_ID) \
		uv run --with boto3 pytest packages/core/tests/test_kb_data_source_live.py -v

# Infra targets operate on the dev stack (see infra/README.md for backend login).
#
# The self-managed S3 backend needs a passphrase even when it is empty, and every
# pulumi command fails without it. Empty is correct today: no secret values live
# in stack config, and the Aurora bedrock_user password is deliberately kept out
# of Pulumi state (scripts/aurora_bootstrap.py writes it straight to Secrets
# Manager). Set a real one before that stops being true:
#   PULUMI_CONFIG_PASSPHRASE=… make infra-up
PULUMI_CONFIG_PASSPHRASE ?=
export PULUMI_CONFIG_PASSPHRASE

# The Lambda artifact for infra/components/api.py: docpipe_core + services/api/
# chat.py + the LOCKED runtime deps — `uv export` from uv.lock, no dev group —
# with wheels for Lambda's arm64 (aarch64-manylinux2014). pydantic-core is
# compiled, so the host's wheels would not load there; the platform and the
# Python version are asserted against the component's architectures/runtime by
# test_lambda_packaging_contract.py. ~35 MB unpacked (cap 250). Gitignored
# (build/, *.zip); `pulumi preview`/`up` with enableChatApi refuse to run
# without it, naming this target.
LAMBDA_BUILD := build/lambda
LAMBDA_ZIP := build/lambda.zip
LAMBDA_PLATFORM ?= aarch64-manylinux2014
LAMBDA_PYTHON ?= 3.12
lambda-zip:
	rm -rf $(LAMBDA_BUILD) $(LAMBDA_ZIP)
	mkdir -p $(LAMBDA_BUILD)
	uv export --package docpipe-core --no-dev --no-hashes --no-emit-project --frozen \
		> $(LAMBDA_BUILD).requirements.txt
	uv pip install --quiet --target $(LAMBDA_BUILD) --python-platform $(LAMBDA_PLATFORM) \
		--python-version $(LAMBDA_PYTHON) --only-binary :all: -r $(LAMBDA_BUILD).requirements.txt
	cp -R packages/core/src/docpipe_core $(LAMBDA_BUILD)/docpipe_core
	cp services/api/chat.py $(LAMBDA_BUILD)/chat.py
	rm -rf $(LAMBDA_BUILD)/bin
	find $(LAMBDA_BUILD) -name '__pycache__' -type d -prune -exec rm -rf {} +
	cd $(LAMBDA_BUILD) && zip -qr ../lambda.zip .
	@du -h $(LAMBDA_ZIP)

infra-preview:
	cd infra && pulumi preview --diff

infra-up:
	cd infra && pulumi up

infra-down:
	cd infra && pulumi destroy

# Build the body-graph edge index from the corpus (PLAN Phase 5b, rung (a)).
# A build artifact, gitignored like status.json — regenerate after every corpus
# re-sync or graph expansion will traverse edges the KB no longer holds.
# Costs nothing: pure local parse, no AWS, no LLM.
# SOURCE defaults to the DEPLOYED corpus, not the local build: measured
# 2026-08-21 they differ by 75 graph chunks (379 local vs 304 ingested, almost
# all the nerve registry), and an index ahead of the KB mints hops that
# Retrieve cannot answer.
KB_BUCKET ?= $(shell PULUMI_CONFIG_PASSPHRASE= pulumi -C infra stack output kb_source_bucket 2>/dev/null)
SOURCE ?= s3://$(KB_BUCKET)/corpus
GRAPH_INDEX ?= .scratch/graph-index.json
graph-index:
	uv run --with boto3 python scripts/graph_index.py --source $(SOURCE) --out $(GRAPH_INDEX)

# Local chat playground ("tester window", PLAN Phase 5b) — a browser page over
# the real chat path: store toggle, single-shot vs agentic, HYBRID, rerank,
# min_evidence, with passages + scores, the tool-call trace, latency and tokens.
#
# Spends real money per ask (retrieve + Converse) and needs the dev stack
# selected: `pulumi -C infra stack select dev`. Ids resolve from stack output;
# override with --s3-kb-id / --aurora-kb-id or the matching env vars.
PORT ?= 8765
playground:
	uv run --with boto3 python scripts/playground.py --port $(PORT)

# The SQL half Pulumi cannot do. Between `enableAurora` and `enableAuroraKb`.
aurora-bootstrap:
	uv run --with boto3 python scripts/aurora_bootstrap.py

kb-agentic-eval:
	uv run --with boto3 python scripts/kb_agentic_eval.py \
		--questions $(QUESTIONS) --kb-id $(KB_ID) --out $(OUT) $(DRY) $(SEARCH) $(FLAGS)
