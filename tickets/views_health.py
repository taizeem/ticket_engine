import time
from django.views import View
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.db import connection
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from tickets.waiting_room import get_redis_client


@method_decorator(csrf_exempt, name="dispatch")
class LivenessProbeView(View):
    """
    Proves process health: returns HTTP 200 with zero external I/O.
    Fails only if the Python ASGI process or thread pool is completely frozen.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        return JsonResponse(
            {
                "status": "alive",
                "request_id": getattr(request, "request_id", None),
            },
            status=200,
        )


@method_decorator(csrf_exempt, name="dispatch")
class ReadinessProbeView(View):
    """
    Proves dependency health: pings PostgreSQL and Redis without multi-statement syntax errors.
    Returns HTTP 503 if connection pool starvation or broker partition occurs.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        start_time = time.time()
        health_status = {"status": "ready", "checks": {}}
        all_healthy = True

        # 1. PostgreSQL Connection & Pool Health Check (single-statement query)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
            health_status["checks"]["database"] = "ok"
        except Exception as e:
            health_status["checks"]["database"] = f"unhealthy: {str(e)}"
            all_healthy = False

        # 2. Redis Waiting Room & Token Bucket Socket Check
        try:
            redis_client = get_redis_client()
            if redis_client.ping():
                health_status["checks"]["redis"] = "ok"
            else:
                health_status["checks"]["redis"] = "unhealthy: no ping response"
                all_healthy = False
        except Exception as e:
            health_status["checks"]["redis"] = f"unhealthy: {str(e)}"
            all_healthy = False

        elapsed_ms = round((time.time() - start_time) * 1000, 2)
        health_status["latency_ms"] = elapsed_ms
        health_status["request_id"] = getattr(request, "request_id", None)

        status_code = 200 if all_healthy else 503
        if not all_healthy:
            health_status["status"] = "degraded"

        return JsonResponse(health_status, status=status_code)