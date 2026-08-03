.PHONY: sync fmt lint test infra-init infra-up infra-down

sync:
	uv sync --all-packages

fmt:
	uv run ruff format packages services
	terraform fmt -recursive infra

lint:
	uv run ruff check packages services
	terraform fmt -check -recursive infra

test:
	uv run pytest

# Infra targets operate on the dev environment.
# Requires infra/envs/dev/backend.hcl and terraform.tfvars (gitignored).
infra-init:
	terraform -chdir=infra/envs/dev init -backend-config=backend.hcl

infra-up:
	terraform -chdir=infra/envs/dev apply

infra-down:
	terraform -chdir=infra/envs/dev destroy
