from django.apps import AppConfig


class AppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app"

    def ready(self):
        import os
        if not os.environ.get("CELERY_BROKER_URL"):
            from pricediff.celery import app, resolve_broker_url
            url = resolve_broker_url()
            app.conf.broker_url = url
            app.conf.result_backend = url
