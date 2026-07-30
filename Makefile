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
