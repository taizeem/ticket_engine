from django.urls import path
from tickets.views import EnterWaitingRoomView, AllocateSeatsView

urlpatterns = [
    path("api/events/<int:event_id>/enter/", EnterWaitingRoomView.as_view(), name="enter_waiting_room"),
    path("api/events/<int:event_id>/allocate/", AllocateSeatsView.as_view(), name="allocate_seats"),
]