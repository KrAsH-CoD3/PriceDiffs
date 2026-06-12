# PriceDiff

Price tracking dashboard — built with Django.

## Setup

```bash
uv sync
uv run python manage.py migrate
```

## Usage

```bash
# Start dev server
uv run python manage.py runserver

# Start Celery worker (required for auto-scraping on add)
uv run celery -A pricediff worker --loglevel=info

# Scrape prices
uv run python manage.py scrape

# Run scheduler (periodic scraping)
uv run python manage.py run_scheduler
```
