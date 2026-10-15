from datetime import timedelta
from typing import List, Optional
from django.db import transaction, connection, models
from django.utils import timezone

from .models import Event, Seat, SeatStatus, Reservation, ReservationStatus
from .exceptions import InsufficientSeatsAvailable, EventNotActive


def allocate_seats_orm(
    event_id: int,
    user_id: str,
    quantity: int,
    hold_duration_seconds: int = 600,
    section: Optional[str] = None,
) -> Reservation:
    """
    Allocates and locks available seats for an event using Django ORM select_for_update(skip_locked=True).
    Recycles expired holds automatically within the lock selection criteria.
    """
    if quantity <= 0:
        raise ValueError("Allocation quantity must be greater than zero.")

    now = timezone.now()
    expires_at = now + timedelta(seconds=hold_duration_seconds)

    with transaction.atomic():
        # Validate event state (read-only check, does not block seat allocations)
        event = Event.objects.filter(id=event_id, is_active=True).first()
        if not event:
            raise EventNotActive(f"Event with id {event_id} does not exist or is inactive.")

        # Candidate query: Look for AVAILABLE seats or HELD seats whose hold expired.
        # SKIP LOCKED allows competing workers to immediately claim unheld rows.
        available_seats_qs = (
            Seat.objects.select_for_update(skip_locked=True)
            .filter(event_id=event_id)
            .filter(
                models.Q(status=SeatStatus.AVAILABLE)
                | (models.Q(status=SeatStatus.HELD) & models.Q(held_until__lt=now))
            )
        )

        if section:
            available_seats_qs = available_seats_qs.filter(section=section)

        # Force evaluation with LIMIT inside transaction
        candidate_seats: List[Seat] = list(available_seats_qs.order_by("id")[:quantity])

        if len(candidate_seats) < quantity:
            # Transaction rolls back automatically, dropping locks on any acquired rows
            raise InsufficientSeatsAvailable(
                f"Requested {quantity} seats, but only {len(candidate_seats)} available."
            )

        # Create temporary reservation
        reservation = Reservation.objects.create(
            user_id=user_id,
            event=event,
            status=ReservationStatus.PENDING,
            expires_at=expires_at,
        )

        # Bulk update locked seats to HELD state
        seat_ids = [seat.id for seat in candidate_seats]
        Seat.objects.filter(id__in=seat_ids).update(
            status=SeatStatus.HELD,
            held_until=expires_at,
            reservation=reservation,
            version=models.F("version") + 1,
            updated_at=now,
        )

    return reservation


def allocate_seats_raw_cte(
    event_id: int,
    user_id: str,
    quantity: int,
    hold_duration_seconds: int = 600,
) -> Reservation:
    """
    Ultra-low latency single-query seat lock and hold assignment.
    Combines SELECT FOR UPDATE SKIP LOCKED with UPDATE in an atomic CTE.
    """
    if quantity <= 0:
        raise ValueError("Allocation quantity must be greater than zero.")

    now = timezone.now()
    expires_at = now + timedelta(seconds=hold_duration_seconds)

    with transaction.atomic():
        event = Event.objects.filter(id=event_id, is_active=True).first()
        if not event:
            raise EventNotActive(f"Event with id {event_id} does not exist or is inactive.")

        reservation = Reservation.objects.create(
            user_id=user_id,
            event=event,
            status=ReservationStatus.PENDING,
            expires_at=expires_at,
        )

        query = """
        WITH candidate_seats AS (
            SELECT id
            FROM ticket_seats
            WHERE event_id = %s
              AND (
                  status = 'AVAILABLE'
                  OR (status = 'HELD' AND held_until < %s)
              )
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE ticket_seats
        SET status = 'HELD',
            held_until = %s,
            reservation_id = %s,
            version = ticket_seats.version + 1,
            updated_at = %s
        FROM candidate_seats
        WHERE ticket_seats.id = candidate_seats.id
        RETURNING ticket_seats.id;
        """

        with connection.cursor() as cursor:
            cursor.execute(
                query,
                [event_id, now, quantity, expires_at, str(reservation.id), now],
            )
            updated_ids = [row[0] for row in cursor.fetchall()]

        if len(updated_ids) < quantity:
            # Shortage roll back: removes reservation record and releases acquired row locks
            transaction.set_rollback(True)
            raise InsufficientSeatsAvailable(
                f"Requested {quantity} seats, but only {len(updated_ids)} available."
            )

    return reservation