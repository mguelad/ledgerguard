.PHONY: up check test plugin stripe-app
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
stripe-app:
	uv run python -m scripts.build_stripe_app --app-id "$(STRIPE_APP_ID)" --origin "$(ORIGIN)"
