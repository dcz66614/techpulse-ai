.PHONY: test lint format

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src/techpulse

format:
	uv run ruff format .
	uv run ruff check --fix .
