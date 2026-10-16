from django.views import View
from django.http import HttpResponse, HttpRequest
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from tickets.models import Event
from tickets.waiting_room.inventory import InventoryManager
from tickets.waiting_room.queue_manager import VirtualWaitingRoom
from tickets.metrics import (
    AVAILABLE_INVENTORY,
    WAITING_ROOM_QUEUE_SIZE,
    CIRCUIT_BREAKER_STATUS,
)


@method_decorator(csrf_exempt, name="dispatch")
class PrometheusMetricsView(View):
    """
    Scrape endpoint for Prometheus. Samples real-time Redis queue depths
    and inventory levels before rendering Prometheus text format.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        # Sample active events to update instantaneous Redis gauges
        active_event_ids = Event.objects.filter(is_active=True).values_list("id", flat=True)[:20]

        for eid in active_event_ids:
            inv_mgr = InventoryManager(event_id=eid)
            w_room = VirtualWaitingRoom(event_id=eid)

            AVAILABLE_INVENTORY.labels(event_id=str(eid)).set(inv_mgr.get_available_count())
            CIRCUIT_BREAKER_STATUS.labels(event_id=str(eid)).set(1 if inv_mgr.is_sold_out() else 0)
            WAITING_ROOM_QUEUE_SIZE.labels(event_id=str(eid)).set(w_room.get_queue_size())

        output = generate_latest()
        return HttpResponse(output, content_type=CONTENT_TYPE_LATEST)