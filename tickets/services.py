from decimal import Decimal
from datetime import timedelta
from typing import List, Optional, Tuple
from django.db import models, transaction, connection
from django.utils import timezone

from tickets.models import Event, Seat, SeatStatus, Reservation, ReservationStatus, PaymentRecord
from tickets.exceptions import (
    InsufficientSeatsAvailable,
    EventNotActive,
    ReservationExpiredError,
    ReservationAlreadyProcessedError,
    InvalidPaymentAmountError,
)


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
        event = Event.objects.filter(id=event_id, is_active=True).first()
        if not event:
            raise EventNotActive(f"Event with id {event_id} does not exist or is inactive.")

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

        candidate_seats: List[Seat] = list(available_seats_qs.order_by("id")[:quantity])

        if len(candidate_seats) < quantity:
            raise InsufficientSeatsAvailable(
                f"Requested {quantity} seats, but only {len(candidate_seats)} available."
            )

        reservation = Reservation.objects.create(
            user_id=user_id,
            event=event,
            status=ReservationStatus.PENDING,
            expires_at=expires_at,
        )

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
            transaction.set_rollback(True)
            raise InsufficientSeatsAvailable(
                f"Requested {quantity} seats, but only {len(updated_ids)} available."
            )

    return reservation


def confirm_reservation_payment(
    reservation_id: str,
    idempotency_key: str,
    payment_reference: str,
    amount: Decimal,
) -> Tuple[Reservation, bool]:
    """
    Finalizes an unconfirmed booking upon successful payment webhook delivery.
    Guarantees strict idempotency: duplicate deliveries return the existing record
    without re-mutating seat or ledger states.
    Returns: (reservation, is_newly_confirmed)
    """
    now = timezone.now()
    is_expired = False
    expired_timestamp = None

    with transaction.atomic():
        # 1. Idempotency Check
        existing_payment = (
            PaymentRecord.objects.select_for_update()
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing_payment:
            return existing_payment.reservation, False

        # 2. Lock Reservation row exclusively
        reservation = (
            Reservation.objects.select_for_update()
            .filter(id=reservation_id)
            .first()
        )
        if not reservation:
            raise ValueError(f"Reservation {reservation_id} not found.")

        # 3. State Invariant Checks
        if reservation.status == ReservationStatus.CONFIRMED:
            PaymentRecord.objects.create(
                idempotency_key=idempotency_key,
                reservation=reservation,
                payment_reference=payment_reference,
                amount=amount,
                status="SUCCESS",
            )
            return reservation, False

        if reservation.status != ReservationStatus.PENDING:
            raise ReservationAlreadyProcessedError(
                f"Reservation {reservation_id} is in status '{reservation.status}' and cannot be confirmed."
            )

        if reservation.expires_at <= now:
            # Mark reservation expired and release seats back to AVAILABLE
            reservation.status = ReservationStatus.EXPIRED
            reservation.save(update_fields=["status", "updated_at"])
            Seat.objects.filter(reservation=reservation, status=SeatStatus.HELD).update(
                status=SeatStatus.AVAILABLE,
                held_until=None,
                reservation=None,
                version=models.F("version") + 1,
                updated_at=now,
            )
            is_expired = True
            expired_timestamp = reservation.expires_at
            # Notice: No exception raised here. The atomic block finishes and commits to PostgreSQL!
        else:
            # 4. Verify Payment Amount against locked seats
            held_seats = list(
                Seat.objects.select_for_update()
                .filter(reservation=reservation, status=SeatStatus.HELD)
            )
            expected_total = sum(seat.price for seat in held_seats)
            if amount != expected_total:
                raise InvalidPaymentAmountError(
                    f"Expected amount {expected_total}, but received {amount}."
                )

            # 5. Atomically transition state: HELD -> BOOKED, PENDING -> CONFIRMED
            reservation.status = ReservationStatus.CONFIRMED
            reservation.updated_at = now
            reservation.save(update_fields=["status", "updated_at"])

            Seat.objects.filter(id__in=[s.id for s in held_seats]).update(
                status=SeatStatus.BOOKED,
                held_until=None,
                version=models.F("version") + 1,
                updated_at=now,
            )

            PaymentRecord.objects.create(
                idempotency_key=idempotency_key,
                reservation=reservation,
                payment_reference=payment_reference,
                amount=amount,
                status="SUCCESS",
            )

    # 6. Raise outside transaction.atomic() so the database updates remain committed
    if is_expired:
        raise ReservationExpiredError(
            f"Reservation {reservation_id} expired at {expired_timestamp.isoformat()}."
        )

    return reservation, True