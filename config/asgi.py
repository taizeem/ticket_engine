import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# Initialize standard Django ASGI application early to ensure AppRegistry is populated
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from tickets.channels.routing import websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        # Standard HTTP traffic handled by Django Views
        "http": django_asgi_app,
        # WebSocket traffic routed to Django Channels
        "websocket": AllowedHostsOriginValidator(
            URLRouter(websocket_urlpatterns)
        ),
    }
)