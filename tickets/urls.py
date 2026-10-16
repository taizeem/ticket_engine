from django.urls import path
from tickets.views import (
    EnterWaitingRoomView,
    AllocateSeatsView,
    ConfirmReservationView,
    PoWChallengeView,
    EventAvailabilityView,
    CancelReservationView,
    )
from tickets.views_health import LivenessProbeView, ReadinessProbeView
urlpatterns = [
    path("healthz/live/", LivenessProbeView.as_view(), name="health_liveness"),
    path("healthz/ready/", ReadinessProbeView.as_view(), name="health_readiness"),
    # Core Engine Endpoints
    path("api/events/<int:event_id>/availability/", EventAvailabilityView.as_view(), name="event_availability"),
    path("api/events/<int:event_id>/challenge/", PoWChallengeView.as_view(), name="pow_challenge"),
    path("api/events/<int:event_id>/enter/", EnterWaitingRoomView.as_view(), name="enter_waiting_room"),
    path("api/events/<int:event_id>/allocate/", AllocateSeatsView.as_view(), name="allocate_seats"),
    path("api/reservations/<uuid:reservation_id>/confirm/", ConfirmReservationView.as_view(), name="confirm_reservation"),
    path("api/reservations/<uuid:reservation_id>/cancel/", CancelReservationView.as_view(), name="cancel_reservation"),
]