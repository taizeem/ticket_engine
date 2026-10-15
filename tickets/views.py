import json
from django.views import View
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from decimal import Decimal, InvalidOperation

from tickets.models import Event, uuid
from tickets.services import allocate_seats_orm, confirm_reservation_payment, allocate_seats_contiguous
from tickets.exceptions import InsufficientSeatsAvailable, EventNotActive, ReservationAlreadyProcessedError, ReservationExpiredError, InvalidPaymentAmountError
from tickets.waiting_room.token_bucket import RedisTokenBucket
from tickets.waiting_room.queue_manager import VirtualWaitingRoom
from tickets.waiting_room.admission import generate_admission_pass, verify_admission_pass

# Global bucket instance: 100 req/sec steady rate with 200 burst capacity
global_token_bucket = RedisTokenBucket(refill_rate=100.0, capacity=200)


@method_decorator(csrf_exempt, name="dispatch")
class EnterWaitingRoomView(View):
    """
    Ingress Gate: Accepts incoming traffic, checks Redis token bucket,
    and either grants an immediate admission pass or enqueues into ZSET.
    """

    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        try:
            payload = json.loads(request.body.decode("utf-8"))
            user_id = str(payload["user_id"]).strip()
            if not user_id:
                raise ValueError("user_id cannot be blank.")
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return JsonResponse({"error": f"Invalid payload: {str(e)}"}, status=400)

        # 1. Attempt token bucket consumption (fast-path admission)
        is_allowed, remaining_tokens = global_token_bucket.consume(event_id=event_id)

        if is_allowed:
            admission_pass = generate_admission_pass(
                event_id=event_id,
                user_id=user_id,
                ttl_seconds=60,
            )
            return JsonResponse(
                {
                    "status": "ADMITTED",
                    "admission_pass": admission_pass,
                    "expires_in_seconds": 60,
                },
                status=200,
            )

        # 2. Burst capacity exhausted: Enqueue user into Redis ZSET waiting room
        waiting_room = VirtualWaitingRoom(event_id=event_id)
        queue_position, is_new = waiting_room.enqueue(user_id=user_id)
        total_waiting = waiting_room.get_queue_size()

        return JsonResponse(
            {
                "status": "QUEUED",
                "queue_position": queue_position,
                "total_waiting": total_waiting,
                "channel_id": f"event_{event_id}_user_{user_id}",
            },
            status=202,
        )


@method_decorator(csrf_exempt, name="dispatch")
class AllocateSeatsView(View):
    """
    Protected Checkout: Requires cryptographically valid X-Admission-Token.
    Supports both contiguous seat allocation and scattered fallback.
    """

    def post(self, request: HttpRequest, event_id: int) -> HttpResponse:
        # 1. Extract and verify admission token
        admission_token = request.headers.get("X-Admission-Token")
        if not admission_token:
            return JsonResponse({"error": "Missing X-Admission-Token header"}, status=401)

        try:
            payload = json.loads(request.body.decode("utf-8"))
            user_id = str(payload["user_id"]).strip()
            quantity = int(payload.get("quantity", 1))
            section = payload.get("section")
            contiguous = bool(payload.get("contiguous", True))
            allow_fallback = bool(payload.get("allow_fallback", True))
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return JsonResponse({"error": f"Invalid payload: {str(e)}"}, status=400)

        # Constant-time signature and expiration verification
        if not verify_admission_pass(
            token=admission_token,
            expected_event_id=event_id,
            expected_user_id=user_id,
        ):
            return JsonResponse({"error": "Invalid or expired admission token"}, status=403)

        # 2. Invoke allocation service based on seating preference
        try:
            if contiguous and quantity > 1:
                reservation = allocate_seats_contiguous(
                    event_id=event_id,
                    user_id=user_id,
                    quantity=quantity,
                    hold_duration_seconds=300,
                    section=section,
                    allow_scattered_fallback=allow_fallback,
                )
            else:
                reservation = allocate_seats_orm(
                    event_id=event_id,
                    user_id=user_id,
                    quantity=quantity,
                    hold_duration_seconds=300,
                    section=section,
                )
        except EventNotActive as e:
            return JsonResponse({"error": str(e)}, status=400)
        except InsufficientSeatsAvailable as e:
            return JsonResponse({"error": str(e)}, status=409)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

        allocated_seats = [
            {
                "id": seat.id,
                "section": seat.section,
                "row": seat.row,
                "seat_number": seat.seat_number,
                "price": str(seat.price),
            }
            for seat in reservation.seats.all()
        ]

        return JsonResponse(
            {
                "reservation_id": str(reservation.id),
                "status": reservation.status,
                "expires_at": reservation.expires_at.isoformat(),
                "seats": allocated_seats,
            },
            status=201,
        )

@method_decorator(csrf_exempt, name="dispatch")
class ConfirmReservationView(View):
    """
    Idempotent Webhook Endpoint: Finalizes checkout upon gateway payment confirmation.
    Expects 'Idempotency-Key' in HTTP headers or JSON payload.
    """

    def post(self, request: HttpRequest, reservation_id: str) -> HttpResponse:
        idempotency_key = (
            request.headers.get("Idempotency-Key")
            or request.headers.get("X-Idempotency-Key")
        )

        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
            if not idempotency_key:
                idempotency_key = payload.get("idempotency_key")

            if not idempotency_key:
                return JsonResponse({"error": "Missing Idempotency-Key header or payload parameter."}, status=400)

            payment_reference = payload.get("payment_reference", f"pay_{uuid.uuid4().hex[:12]}")
            amount = Decimal(str(payload["amount"]))
        except (KeyError, ValueError, InvalidOperation, json.JSONDecodeError) as e:
            return JsonResponse({"error": f"Invalid confirmation payload: {str(e)}"}, status=400)

        try:
            reservation, is_newly_confirmed = confirm_reservation_payment(
                reservation_id=reservation_id,
                idempotency_key=idempotency_key,
                payment_reference=payment_reference,
                amount=amount,
            )
        except ReservationExpiredError as e:
            return JsonResponse({"error": str(e)}, status=410)  # 410 Gone
        except (ReservationAlreadyProcessedError, InvalidPaymentAmountError) as e:
            return JsonResponse({"error": str(e)}, status=409)  # 409 Conflict
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=404)

        confirmed_seats = [
            {
                "id": seat.id,
                "section": seat.section,
                "row": seat.row,
                "seat_number": seat.seat_number,
                "ticket_code": f"TICK-{seat.id}-{reservation.id.hex[:8].upper()}",
            }
            for seat in reservation.seats.all()
        ]

        return JsonResponse(
            {
                "status": "CONFIRMED",
                "is_newly_confirmed": is_newly_confirmed,
                "reservation_id": str(reservation.id),
                "payment_reference": payment_reference,
                "tickets": confirmed_seats,
            },
            status=200 if not is_newly_confirmed else 201,
        )