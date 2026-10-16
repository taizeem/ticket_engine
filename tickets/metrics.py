from prometheus_client import Counter, Gauge, Histogram

# 1. Latency Histograms
ALLOCATION_DURATION_SECONDS = Histogram(
    "ticket_allocation_duration_seconds",
    "Time spent executing SELECT FOR UPDATE SKIP LOCKED and row mutation in PostgreSQL",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

# 2. Operational Gauges
WAITING_ROOM_QUEUE_SIZE = Gauge(
    "ticket_waiting_room_queue_size",
    "Instantaneous count of clients waiting in the Redis ZSET queue",
    ["event_id"],
)

AVAILABLE_INVENTORY = Gauge(
    "ticket_available_inventory",
    "Current unallocated inventory count cached in Redis",
    ["event_id"],
)

CIRCUIT_BREAKER_STATUS = Gauge(
    "ticket_circuit_breaker_tripped",
    "Flag indicating if the sold-out circuit breaker is tripped (1=Tripped, 0=Active)",
    ["event_id"],
)

# 3. Security & Transaction Counters
POW_VERIFICATIONS_TOTAL = Counter(
    "ticket_pow_verifications_total",
    "Total Proof-of-Work verifications evaluated at ingress",
    ["status"],  # "valid", "invalid", "expired", "replay"
)

RESERVATIONS_CONFIRMED_TOTAL = Counter(
    "ticket_reservations_confirmed_total",
    "Total payments successfully processed and converted from HELD to BOOKED",
)

SEATS_RECLAIMED_TOTAL = Counter(
    "ticket_seats_reclaimed_total",
    "Total expired hold seats recycled back to available inventory by the sweeper",
)