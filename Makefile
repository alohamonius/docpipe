.PHONY: sync fmt lint typecheck test status infra-preview infra-up infra-down

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
infra-preview:
	cd pulumi && pulumi preview

infra-up:
	cd pulumi && pulumi up

infra-down:
	cd pulumi && pulumi destroy
