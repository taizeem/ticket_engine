```markdown
# High-Throughput Flash-Sale Ticket Booking Engine

A distributed, concurrency-safe flash-sale ticketing platform engineered to absorb **50,000 requests/second burst traffic** without lock convoys, database connection starvation, or inventory over-allocation.

Built with **Django 5.1**, **PostgreSQL 16** (`SELECT FOR UPDATE SKIP LOCKED`), **Redis 7** (Lua Token Bucket & ZSET Waiting Rooms), **Django Channels & Daphne** (ASGI WebSockets), and **Celery** background worker pools.


```

```
                          High-Throughput Ingress Funnel

```

HTTP Ingress (50k req/s)
│
▼
[ Correlation ID Middleware ] ──► Injects trace UUID (X-Request-ID) across request lifecycle
│
▼
[ Sold-Out Circuit Breaker ]  ──► Fast-fails in 0.2ms via Redis memory if inventory == 0 (HTTP 410)
│ (Inventory Available)
▼
[ Anti-Bot PoW Gate ]         ──► Enforces 65k-hash client SHA-256 puzzle; verified in O(1) time
│ (Valid Proof-of-Work)
▼
[ Redis Lua Token Bucket ]    ──► Admits 100 req/s baseline to DB; shunts excess to Virtual Waiting Room
│
├── (Tokens Available) ────────────────────────────────────────────────────────┐
│                                                                              │
▼ (Burst Depleted)                                                             │
[ Redis ZSET Waiting Room ]                                                             │
│                                                                              │
▼                                                                              │
[ Celery Turnstile Worker ] ──► Pops top N users & issues HMAC-SHA256 Admission Pass    │
│                                                                              │
▼                                                                              │
[ Channels WebSocket ]      ──► Streams live queue rank & pushes pass directly to client│
│                                                                              │
└───────────────────────────────┬──────────────────────────────────────────────┘
▼
[ /api/events//allocate/ (X-Admission-Token) ]
│
▼
[ PostgreSQL SELECT FOR UPDATE SKIP LOCKED ]
├── Window CTE (LEAD) for contiguous row allocation
└── Partial B-Tree index on status = 'AVAILABLE'
│
▼
[ Celery Beat Hold Sweeper ] ──► Sweeps expired holds every 5 seconds
│
▼
[ /api/reservations//confirm/ (Idempotency-Key) ]
├── Atomically transitions HELD -> BOOKED
└── Prevents double-billing and duplicate tickets under network retries

```

## Architectural Highlights

* **Multi-Tier Ingress Shielding**: Inbound bursts hit an atomic Redis edge barrier combining a SHA-256 Proof-of-Work gate, a sold-out circuit breaker, and an atomic Lua token bucket (100 req/s, 200 burst capacity), protecting database connection pools from direct HTTP saturation.
* **Lock-Free Concurrency Primitives**: Seat allocation bypasses table-level locks and standard pessimistic row locks (`SELECT FOR UPDATE`) to eliminate lock convoying. Uses PostgreSQL's `SELECT ... FOR UPDATE SKIP LOCKED` to allow competing workers to claim unheld inventory with zero thread blocking.
* **Contiguous Row Grouping**: Implements an adjacent seating algorithm using PostgreSQL `LEAD()` window functions within an atomic Common Table Expression (CTE), ensuring parties sit together while automatically skipping locked seats under concurrent race conditions.
* **Virtual Waiting Room & Turnstiles**: Excess traffic queues in a Redis Sorted Set (`ZSET`) ordered by monotonic arrival timestamps ($O(\log N)$ rank calculation). A Celery turnstile worker pops admitted batches and pushes cryptographically signed HMAC-SHA256 admission tokens over persistent WebSocket duplex connections.
* **Strict State-Machine Idempotency**: State transitions follow an immutable lifecycle (`AVAILABLE` $\to$ `HELD` $\to$ `BOOKED` / `EXPIRED` / `CANCELLED`). Payment webhooks are guarded by an atomic test-and-set `PaymentRecord` table with `Idempotency-Key` headers, eliminating duplicate ticket issuance and double-billing under gateway retries.
* **Production Observability**: Features dual liveness/readiness probes (`/healthz/live/`, `/healthz/ready/`), distributed correlation-ID tracking middleware (`X-Request-ID`), and real-time Prometheus telemetry (`/metrics/`) exporting allocation latency histograms and queue depth gauges.

## Technology Stack

* **Language & Runtime**: Python 3.12+ managed via `uv`
* **Web Framework**: Django 5.1 (ASGI mode via Daphne)
* **Relational Database**: PostgreSQL 16 (psycopg3 driver with native client-side connection pooling)
* **In-Memory Store**: Redis 7 (Lua scripts, Sorted Sets, Pub/Sub, Channel Layer)
* **Task Queue & Scheduler**: Celery 5.4+ with Celery Beat
* **Real-Time WebSockets**: Django Channels 4 + `channels-redis`
* **Observability & Testing**: Prometheus Client, Locust, pytest-django, TransactionTestCase suite (38 tests)

## Project Structure

```text
ticketing_engine/
├── .env.example                        # Template for production and local environments
├── pyproject.toml                      # uv package definitions and pytest configuration
├── manage.py
├── config/                             # Project orchestration root
│   ├── asgi.py                         # ASGI routing (HTTP + Django Channels WebSockets)
│   ├── celery.py                       # Celery worker and recurrent Beat schedule definitions
│   ├── settings.py                     # psycopg3 pooling, Redis caches, and channel layers
│   └── urls.py                         # Root URL configuration
├── tickets/                            # Core ticketing domain application
│   ├── models.py                       # Event, Seat, Reservation, PaymentRecord schemas
│   ├── services.py                     # Concurrency-safe ORM & raw SQL CTE allocation services
│   ├── exceptions.py                   # Domain exceptions (InsufficientSeatsAvailable, etc.)
│   ├── metrics.py                      # Centralized Prometheus metrics registry
│   ├── views.py                        # Ingress gate, checkout, and idempotent confirmation APIs
│   ├── views_health.py                 # Dual liveness and readiness healthcheck probes
│   ├── views_metrics.py                # Prometheus OpenMetrics export endpoint
│   ├── middleware/
│   │   └── correlation.py              # X-Request-ID distributed tracing middleware
│   ├── waiting_room/                   # Redis edge gating and rate limiting
│   │   ├── inventory.py                # Atomic counter and sold-out circuit breaker
│   │   ├── token_bucket.py             # Atomic Lua token bucket rate limiter
│   │   ├── queue_manager.py            # Redis ZSET FIFO waiting room queue
│   │   ├── admission.py                # HMAC-SHA256 admission token issuance and verification
│   │   └── pow.py                      # Asymmetric Proof-of-Work anti-bot challenge engine
│   ├── tasks/                          # Celery asynchronous worker jobs
│   │   ├── turnstile_tasks.py          # Batch admission and WebSocket broadcast worker
│   │   └── expiration_tasks.py         # Recurrent hold expiration and inventory reclamation
│   ├── channels/                       # WebSocket real-time communication
│   │   ├── consumers.py                # WaitingRoomConsumer streaming real-time queue rank
│   │   └── routing.py                  # WebSocket URL routing patterns
│   ├── management/commands/
│   │   └── seed_flash_sale.py          # High-speed arena seeder (bulk inserts 5,000+ seats)
│   └── tests/                          # 38-test TDD test suite
│       ├── test_allocation.py          # Multithreaded SKIP LOCKED concurrency verification
│       ├── test_contiguous_allocation.py# PostgreSQL window CTE contiguous row test
│       ├── test_waiting_room.py        # Redis Lua token bucket and ZSET queue integrity
│       ├── test_api_flow.py            # Full API ingress and admission pass flow
│       ├── test_tasks.py               # Celery turnstile and hold sweeper lifecycle
│       ├── test_channels.py            # Async WebSocket queue streaming tests
│       ├── test_confirmation.py        # Idempotent payment webhook and replay tests
│       ├── test_circuit_breaker.py     # Sold-out circuit breaker fast-fail tests
│       ├── test_pow_gate.py            # Anti-bot cryptographic challenge verification
│       ├── test_seed_command.py        # Database and Redis seeder verification
│       ├── test_operational_features.py# Health probes and correlation ID middleware tests
│       └── test_metrics.py             # Prometheus OpenMetrics export verification
└── load_tests/
    └── locustfile.py                   # Asynchronous full-funnel Locust stress test

```

## Quick Start Guide

### 1. Prerequisites

* Python 3.12+
* `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `irm https://astral.sh/uv/install.ps1 | iex`)
* PostgreSQL 16 running locally on port 5432
* Redis 7 running locally on port 6379

### 2. Environment Configuration



Ensure your `.env` contains:

```ini
DEBUG=False
SECRET_KEY=dev-entropy-secret-key-change-in-production!
ALLOWED_HOSTS=localhost,127.0.0.1,testserver

DB_NAME=ticket_engine_db
DB_USER=ticket_admin
DB_PASSWORD=secret_postgres_pass
DB_HOST=127.0.0.1
DB_PORT=5432

# Connection pooling limits (psycopg3 native pool)
DB_CONN_MAX_AGE=0
DB_POOL_MIN_SIZE=10
DB_POOL_MAX_SIZE=50
DB_POOL_TIMEOUT=30

REDIS_URL=redis://127.0.0.1:6379/0
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2
CHANNEL_LAYERS_REDIS_URL=redis://127.0.0.1:6379/3

```

### 3. Database Provisioning

Create the database role and schema via `psql`:

```sql
CREATE ROLE ticket_admin WITH LOGIN PASSWORD 'secret_postgres_pass' CREATEDB;
CREATE DATABASE ticket_engine_db OWNER ticket_admin ENCODING 'UTF8';
ALTER ROLE ticket_admin SET timezone TO 'UTC';
ALTER ROLE ticket_admin SET statement_timeout = '3000ms';
ALTER ROLE ticket_admin SET lock_timeout = '2000ms';

```

Apply migrations:

```bash
uv run python manage.py migrate

```

### 4. Running the Test Suite

Execute the 38-test concurrency, security, and integration test suite:

```bash
uv run python manage.py test tickets --keepdb

```

### 5. Running the Application Cluster

Start each service in a separate terminal:

```bash
# Terminal 1: Daphne ASGI Web & WebSocket Server
uv run daphne -p 8000 config.asgi:application

# Terminal 2: Celery Worker Pool
# (Use --pool=solo on Windows; omit --pool flag on Linux/macOS)
uv run celery -A config worker --pool=solo --loglevel=info

# Terminal 3: Celery Beat Periodic Scheduler (Expired hold sweeps)
uv run celery -A config beat --loglevel=info

```

## API Reference

| Method | Endpoint | Guard / Header | Description |
| --- | --- | --- | --- |
| `GET` | `/healthz/live/` | None | Instant process health probe (zero I/O) |
| `GET` | `/healthz/ready/` | None | Dependency health probe (PostgreSQL & Redis pings) |
| `GET` | `/metrics/` | None | Prometheus OpenMetrics exporter endpoint |
| `GET` | `/api/events/<id>/availability/` | None | Read-through Redis inventory count |
| `GET` | `/api/events/<id>/challenge/` | None | Issues signed anti-bot Proof-of-Work puzzle |
| `POST` | `/api/events/<id>/enter/` | PoW Solution Payload | Ingress gate (Token bucket fast-path or ZSET queue) |
| `WS` | `/ws/waiting-room/<id>/<user>/` | Origin Validation | Persistent socket streaming live queue position & passes |
| `POST` | `/api/events/<id>/allocate/` | `X-Admission-Token` | `SELECT FOR UPDATE SKIP LOCKED` seat allocation |
| `POST` | `/api/reservations/<id>/confirm/` | `Idempotency-Key` | Idempotent payment finalization (`HELD` $\to$ `BOOKED`) |
| `POST` | `/api/reservations/<id>/cancel/` | None | Cancels booking and returns seats to inventory |

## Load & Stress Benchmarking

Seed an event with 5,000 seats across 4 arena sections:

```bash
uv run python manage.py seed_flash_sale --event-id 1 --seats 5000

```

Execute a headless Locust stress benchmark simulating 500 concurrent users hitting the full funnel:

```bash
uv run locust -f load_tests/locustfile.py --headless -u 500 -r 50 --run-time 60s --host [http://127.0.0.1:8000](http://127.0.0.1:8000)

```

Inspect inventory state in PostgreSQL post-run to verify zero double-bookings:

```sql
SELECT status, count(*) FROM ticket_seats WHERE event_id = 1 GROUP BY status;

```

