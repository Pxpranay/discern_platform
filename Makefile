.PHONY: up down reset env migrate test shell worker bootstrap

up: env    ## Start everything — database, app, worker — at http://localhost:8000
	docker compose up --build

down:      ## Stop the stack, keep the data
	docker compose down

reset:     ## Stop and delete the database volume, so the next `make up` reloads the demo
	docker compose down -v

env:       ## Create .env from .env.example if it does not exist yet
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

bootstrap: ## Migrate, seed roles and login, load the demo project if empty
	python manage.py bootstrap

migrate:
	python manage.py migrate

test:      ## Run the full suite (needs Postgres)
	python -m pytest

test-ceiling:  ## Just the invariant that protects the design
	python -m pytest tests/test_ceiling_properties.py tests/test_concurrency.py -v

worker:
	celery -A config worker -l info

demo:      ## Run the end-to-end walkthrough against a real database
	python manage.py demo

run:       ## Serve the app at http://localhost:8000
	python manage.py runserver 0.0.0.0:8000

seed:      ## Default roles, demo login, and a worked project from the real BOQ files
	python manage.py seed_roles
	python manage.py seed_login
	python manage.py demo
