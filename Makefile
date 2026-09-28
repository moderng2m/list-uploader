# List Uploader — common tasks. Run `make help`.
ENV ?= dev
CDK := npx -y aws-cdk@2
# Extra CDK context, e.g. CDK_ARGS="-c alert_emails=you@example.org"
CDK_ARGS ?=

.PHONY: help install lint typecheck test fixtures synth web-dev web-build web-build-live diff deploy fire-alarms verify-archive clean

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
	$(CDK) synth -c env=$(ENV) $(CDK_ARGS) --quiet

web-dev:  ## Run the frontend against the in-browser mock API
	cd frontend && npm run dev

web-build:  ## Build the demo frontend (mock API) into frontend/dist
	cd frontend && npm run build

web-build-live:  ## Build the frontend for a deployment (real API, Cognito sign-in)
	cd frontend && VITE_API_MODE=live npm run build

diff: web-build-live  ## cdk diff against your AWS account
	$(CDK) diff -c env=$(ENV) $(CDK_ARGS)

deploy: web-build-live  ## Deploy all stacks to ENV (dev only from the personal account)
	@test "$(ENV)" = "dev" || (echo "Refusing to deploy $(ENV) from here; only dev is allowed." && exit 1)
	$(CDK) deploy -c env=$(ENV) $(CDK_ARGS) --all

fire-alarms:  ## Plan fault injection for every alarm (add EXECUTE=1 to fire them; dev only)
	uv run python -m ops.fire_alarms --env $(ENV) $(if $(EXECUTE),--execute,)

verify-archive:  ## Check audit archive delivery and Athena access: JOB=j_... [DENIED=arn]
	uv run python -m ops.verify_archive --env $(ENV) --job-id $(JOB) $(if $(DENIED),--denied-principal $(DENIED),)

clean:
	rm -rf cdk.out frontend/dist .pytest_cache .mypy_cache .ruff_cache
