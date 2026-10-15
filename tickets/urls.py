from django.urls import path
from tickets.views import (
    EnterWaitingRoomView,
    AllocateSeatsView,
    ConfirmReservationView,
    PoWChallengeView,
    EventAvailabilityView,
    )
urlpatterns = [
    path("api/events/<int:event_id>/availability/", EventAvailabilityView.as_view(), name="event_availability"),
    path("api/events/<int:event_id>/challenge/", PoWChallengeView.as_view(), name="pow_challenge"),
    path("api/events/<int:event_id>/enter/", EnterWaitingRoomView.as_view(), name="enter_waiting_room"),
    path("api/events/<int:event_id>/allocate/", AllocateSeatsView.as_view(), name="allocate_seats"),
    path("api/reservations/<uuid:reservation_id>/confirm/", ConfirmReservationView.as_view(), name="confirm_reservation"),
]