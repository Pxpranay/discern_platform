.PHONY: up down migrate test shell worker

up:        ## Start the stack
	docker compose up --build

down:
	docker compose down

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
