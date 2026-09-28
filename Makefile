# List Uploader — common tasks. Run `make help`.
ENV ?= dev
CDK := npx -y aws-cdk@2

.PHONY: help install lint typecheck test fixtures synth web-dev web-build diff deploy clean

help:  ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install:  ## Install Python (uv) and frontend (npm) dependencies
	uv sync
	cd frontend && npm ci

lint:  ## Ruff lint + format check, mypy, frontend typecheck
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	cd frontend && npm run typecheck

typecheck: lint

test:  ## Backend + infra tests (pytest/moto) and frontend tests (vitest)
	uv run pytest
	cd frontend && npm test

fixtures:  ## Regenerate synthetic sample files and the downloadable template
	uv run python backend/tests/fixtures/make_synthetic.py

synth:  ## cdk synth for ENV (default dev); bundles Lambda code locally
	$(CDK) synth -c env=$(ENV) --quiet

web-dev:  ## Run the frontend against the in-browser mock API
	cd frontend && npm run dev

web-build:  ## Build the frontend into frontend/dist (deployed by the Web stack)
	cd frontend && npm run build

diff: web-build  ## cdk diff against your AWS account
	$(CDK) diff -c env=$(ENV)

deploy: web-build  ## Deploy all stacks to ENV (dev only from the personal account)
	@test "$(ENV)" = "dev" || (echo "Refusing to deploy $(ENV) from here; only dev is allowed." && exit 1)
	$(CDK) deploy -c env=$(ENV) --all

clean:
	rm -rf cdk.out frontend/dist .pytest_cache .mypy_cache .ruff_cache
