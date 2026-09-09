.PHONY: up check test plugin
up:
	python3 scripts/configure_local.py
	docker compose up --build -d
check:
	uv run --extra dev ruff check .
	uv run --extra dev ruff format --check .
	uv run --extra dev mypy apps modules packages workers
	uv run python scripts/check_contracts.py
	uv run python scripts/check_deployment_settings.py
test:
	uv run --extra dev pytest
plugin:
	uv run python scripts/build_plugin.py --origin "$(ORIGIN)"
