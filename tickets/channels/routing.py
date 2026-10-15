from django.urls import path
from tickets.channels.consumers import WaitingRoomConsumer

websocket_urlpatterns = [
    path(
        "ws/waiting-room/<int:event_id>/<str:user_id>/",
        WaitingRoomConsumer.as_asgi(),
    ),
]