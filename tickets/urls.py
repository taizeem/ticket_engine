from django.urls import path
from tickets.views import EnterWaitingRoomView, AllocateSeatsView, ConfirmReservationView
urlpatterns = [
    path("api/events/<int:event_id>/enter/", EnterWaitingRoomView.as_view(), name="enter_waiting_room"),
    path("api/events/<int:event_id>/allocate/", AllocateSeatsView.as_view(), name="allocate_seats"),
    path("api/reservations/<uuid:reservation_id>/confirm/", ConfirmReservationView.as_view(), name="confirm_reservation"),
]