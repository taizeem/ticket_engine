import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("ticketing_engine")

# Namespace 'CELERY' maps all Celery config keys to Django settings (e.g., CELERY_BROKER_URL)
app.config_from_object("django.conf:settings", namespace="CELERY")

# Automatically discover tasks in all installed apps (tickets/tasks/*.py)
app.autodiscover_tasks()