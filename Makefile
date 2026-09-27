# Thin wrappers around uv / fitme. Run `make` for the list.
# Fitme reads real environment variables only (A§1); it never loads .env itself. When a
# .env file exists in the repo root, load it for these dev commands via uv. Tests must stay
# independent of .env, so this is applied only to $(FITME), never to `test`.
FITME := uv run $(if $(wildcard .env),--env-file .env) fitme
OUT ?= fitme-export.json

.DEFAULT_GOAL := help
.PHONY: help install lock migrate serve activate test lint fmt typecheck catalog check export delete purge llm-eval clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (incl. dev) into .venv
	uv sync

lock: ## Update uv.lock
	uv lock

migrate: ## Apply pending SQL migrations
	$(FITME) db upgrade

serve: ## Run bot + web + retention job
	$(FITME) serve

activate: ## Print a one-time Telegram activation code
	$(FITME) activate

test: ## Run tests
	uv run pytest

lint: ## Lint with ruff
	uv run ruff check .

fmt: ## Format and autofix with ruff
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Type-check with mypy
	uv run mypy src

catalog: ## Validate catalog + locales, print content_version
	$(FITME) catalog check

check: lint typecheck catalog test ## lint + typecheck + catalog + test (run before commit)

export: ## Export all user data to $(OUT)
	$(FITME) export --out $(OUT)

delete: ## DELETE the user and ALL data permanently (no undo; run 'make export' first)
	$(FITME) delete --yes

purge: ## Run retention now
	$(FITME) purge

llm-eval: ## Run LLM eval fixtures (spends money; AGENT=, MODEL= optional; add YES=1 to confirm)
	$(FITME) llm eval $(if $(AGENT),--agent $(AGENT)) $(if $(MODEL),--model $(MODEL)) $(if $(YES),--yes)

clean: ## Remove caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
