import os
from pathlib import Path
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pricediff.settings")

_redis_server = None


def resolve_broker_url() -> str:
    global _redis_server
    url = os.environ.get("CELERY_BROKER_URL", "")
    if url:
        return url
    try:
        import redis as _r
        _r.Redis(host="localhost", port=6379, socket_connect_timeout=1).ping()
        return "redis://localhost:6379/0"
    except Exception:
        pass
    import redislite as _rl
    _redis_server = _rl.Redis(
        str(Path(__file__).resolve().parent.parent / "data" / "celery_redis.db"),
    )
    return f"redis+socket://{_redis_server.socket_file}"


app = Celery("pricediff")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.broker_url = os.environ.get("CELERY_BROKER_URL", "")
app.conf.result_backend = app.conf.broker_url
app.autodiscover_tasks()
