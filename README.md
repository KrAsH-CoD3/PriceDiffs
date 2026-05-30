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

# Scrape prices
uv run python manage.py scrape

# Run scheduler (periodic scraping)
uv run python manage.py run_scheduler
```
