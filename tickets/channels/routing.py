from django.urls import re_path
from tickets.channels.consumers import WaitingRoomConsumer

websocket_urlpatterns = [
    re_path(
        r"^ws/waiting-room/(?P<event_id>\d+)/(?P<user_id>[\w-]+)/$",
        WaitingRoomConsumer.as_asgi(),
    ),
]