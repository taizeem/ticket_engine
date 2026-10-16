from decimal import Decimal
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tickets.models import Event, Seat, SeatStatus
from tickets.waiting_room import get_redis_client
from tickets.waiting_room.inventory import InventoryManager
from tickets.waiting_room.queue_manager import VirtualWaitingRoom


class Command(BaseCommand):
    help = "Seeds a high-capacity flash-sale event and initializes Redis inventory state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--event-id",
            type=int,
            default=1,
            help="Target Event ID to create or reset (default: 1).",
        )
        parser.add_argument(
            "--seats",
            type=int,
            default=5000,
            help="Total number of seats to provision (default: 5000).",
        )
        parser.add_argument(
            "--seats-per-row",
            type=int,
            default=25,
            help="Number of contiguous seats per row (default: 25).",
        )

    def handle(self, *args, **options):
        event_id = options["event_id"]
        total_seats = options["seats"]
        seats_per_row = options["seats_per_row"]
        now = timezone.now()

        with transaction.atomic():
            # Wipe existing event state to guarantee a clean benchmark baseline
            Event.objects.filter(id=event_id).delete()

            event = Event.objects.create(
                id=event_id,
                name=f"Global Flash Sale Benchmark #{event_id}",
                slug=f"flash-sale-benchmark-{event_id}",
                sale_start=now - timedelta(minutes=10),
                sale_end=now + timedelta(days=1),
                total_seats=total_seats,
                is_active=True,
            )

            sections = ["Arena-A", "Arena-B", "Grandstand-C", "VIP-D"]
            seats_to_create = []

            for i in range(total_seats):
                section = sections[(i // 500) % len(sections)]
                row_num = (i // seats_per_row) + 1
                seat_num = (i % seats_per_row) + 1
                price = Decimal("250.00") if section == "VIP-D" else Decimal("120.00")

                seats_to_create.append(
                    Seat(
                        event=event,
                        section=section,
                        row=f"R{row_num}",
                        seat_number=seat_num,
                        price=price,
                        status=SeatStatus.AVAILABLE,
                    )
                )

            # Batch insert in chunks of 2,500 to prevent PostgreSQL packet buffer bloat
            Seat.objects.bulk_create(seats_to_create, batch_size=2500)

        # Reset Redis state (Token Bucket, Waiting Room ZSET, Circuit Breaker, Inventory)
        redis_client = get_redis_client()
        redis_client.delete(f"tb:event:{event_id}")
        VirtualWaitingRoom(event_id=event_id).clear()

        inv_mgr = InventoryManager(event_id=event_id)
        inv_mgr.reset()
        synced_count = inv_mgr.sync_from_db()

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded Event ID={event.id} with {synced_count} seats and synced Redis inventory."
            )
        )