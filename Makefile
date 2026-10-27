.PHONY: test lint format ci secrets

test:
	uv run pytest tests/unit tests/contract -q --cov=src/techpulse --cov-report=term-missing

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff format .
	uv run ruff check --fix .

secrets:
	uv run detect-secrets-hook --baseline .secrets.baseline --exclude-files 'fake_secret_sample\.txt' $$(git -c core.quotePath=false ls-files)

ci:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest tests/unit tests/contract -q --cov=src/techpulse --cov-report=term-missing
	uv run bandit -q -r src
	uv run pip-audit
	uv run detect-secrets-hook --baseline .secrets.baseline --exclude-files 'fake_secret_sample\.txt' $$(git -c core.quotePath=false ls-files)
