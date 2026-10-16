from celery import shared_task
from django.db import transaction, models
from django.utils import timezone
from tickets.models import Seat, SeatStatus, Reservation, ReservationStatus
from tickets.metrics import SEATS_RECLAIMED_TOTAL


@shared_task(name="tickets.tasks.expiration_tasks.reclaim_expired_seat_holds")
def reclaim_expired_seat_holds(batch_size: int = 500) -> int:
    """
    Periodic task to reclaim seats from uncompleted checkouts whose TTL elapsed.
    Resets seats to AVAILABLE and transitions reservation to EXPIRED.
    Returns: count of reclaimed seats.
    """
    now = timezone.now()

    with transaction.atomic():
        # 1. Fetch expired pending reservations (bounded by batch_size)
        expired_reservations = list(
            Reservation.objects.select_for_update(skip_locked=True)
            .filter(
                status=ReservationStatus.PENDING,
                expires_at__lte=now,
            )
            .order_by("expires_at")[:batch_size]
        )

        if not expired_reservations:
            return 0

        reservation_ids = [res.id for res in expired_reservations]

        # 2. Reset associated seats back to AVAILABLE inventory
        reclaimed_seats_count = (
            Seat.objects.filter(reservation_id__in=reservation_ids, status=SeatStatus.HELD)
            .update(
                status=SeatStatus.AVAILABLE,
                held_until=None,
                reservation=None,
                version=models.F("version") + 1,
                updated_at=now,
            )
        )

        # 3. Transition reservations to EXPIRED
        Reservation.objects.filter(id__in=reservation_ids).update(
            status=ReservationStatus.EXPIRED,
            updated_at=now,
        )
    if reclaimed_seats_count > 0:
        SEATS_RECLAIMED_TOTAL.inc(reclaimed_seats_count)
    return reclaimed_seats_count