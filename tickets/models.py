import uuid
from django.db import models
from django.utils import timezone


class Event(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    sale_start = models.DateTimeField(db_index=True)
    sale_end = models.DateTimeField()
    total_seats = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ticket_events"

    def __str__(self):
        return self.name


class ReservationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Payment"
    CONFIRMED = "CONFIRMED", "Confirmed"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"


class Reservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=128, db_index=True)
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    status = models.CharField(
        max_length=16,
        choices=ReservationStatus.choices,
        default=ReservationStatus.PENDING,
        db_index=True,
    )
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ticket_reservations"
        indexes = [
            models.Index(fields=["event", "status", "expires_at"]),
        ]

    def is_expired(self) -> bool:
        return self.status == ReservationStatus.PENDING and self.expires_at <= timezone.now()


class SeatStatus(models.TextChoices):
    AVAILABLE = "AVAILABLE", "Available"
    HELD = "HELD", "Held (Temporary Hold)"
    BOOKED = "BOOKED", "Booked"


class Seat(models.Model):
    id = models.BigAutoField(primary_key=True)
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="seats",
    )
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="seats",
    )
    section = models.CharField(max_length=32)
    row = models.CharField(max_length=16)
    seat_number = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=16,
        choices=SeatStatus.choices,
        default=SeatStatus.AVAILABLE,
    )
    held_until = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ticket_seats"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "section", "row", "seat_number"],
                name="unique_event_seat_location",
            )
        ]
        indexes = [
            # Partial index: only indexes unallocated inventory.
            models.Index(
                fields=["event", "id"],
                name="idx_seats_available_partial",
                condition=models.Q(status="AVAILABLE"),
            ),
            # Composite index for TTL hold sweeps and reservation lookups
            models.Index(
                fields=["event", "status", "held_until"],
                name="idx_seats_hold_expiry",
            ),
        ]

    def __str__(self):
        return f"{self.event.name} - Sec {self.section} Row {self.row} #{self.seat_number}"


class PaymentRecord(models.Model):
    id = models.BigAutoField(primary_key=True)
    idempotency_key = models.CharField(max_length=255, unique=True, db_index=True)
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="payment_records",
    )
    payment_reference = models.CharField(max_length=255, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=32, default="SUCCESS")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ticket_payment_records"
        indexes = [
            models.Index(fields=["reservation", "status"]),
        ]

    def __str__(self):
        return f"Payment {self.payment_reference} ({self.status}) for {self.reservation_id}"