

```markdown
# High-Throughput Flash-Sale Ticket Booking Engine

A distributed, concurrency-hardened flash-sale ticketing platform engineered to sustain **50,000 requests/second burst traffic** without lock convoys, database connection starvation, or inventory over-allocation.

Built with **Django 5.1**, **PostgreSQL 16** (`SELECT FOR UPDATE SKIP LOCKED`), **Redis 7** (Lua Token Bucket & ZSET Virtual Waiting Rooms), **Django Channels & Daphne** (ASGI WebSockets), and **Celery** background worker pools.

## 1. High-Level Architecture Diagram


```

┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       DISTRIBUTED TOPOLOGY                                        │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘

```
                              [ Inbound Traffic: 50,000 req/s ]
                                              │
                                              ▼
                                  [ Nginx / Cloudflare ALB ]
                                              │
                     ┌────────────────────────┴────────────────────────┐
                     ▼                                                 ▼
         [ Daphne ASGI Instance 1 ]                        [ Daphne ASGI Instance 2 ]
         (HTTP API + WebSockets)                           (HTTP API + WebSockets)
                     │                                                 │
                     └────────────────────────┬────────────────────────┘
                                              │
          ┌───────────────────────────────────┼───────────────────────────────────┐
          ▼                                   ▼                                   ▼
┌───────────────────┐               ┌───────────────────┐               ┌───────────────────┐
│    Redis DB 0     │               │   Redis DB 1 & 2  │               │    Redis DB 3     │
│   (Edge Guards)   │               │  (Celery Broker)  │               │ (Channels Layer)  │
├───────────────────┤               ├───────────────────┤               ├───────────────────┤
│ • Sold-Out Breaker│               │ • DB 1: Task Queue│               │ • WebSocket Groups│
│ • PoW Nonce Store │               │ • DB 2: Results   │               │ • Real-time Push  │
│ • Lua Token Bucket│               └─────────┬─────────┘               └───────────────────┘
│ • ZSET Queue Room │                         │
└───────────────────┘                         ▼
                                   ┌─────────────────────┐
                                   │ Celery Worker Pool  │
                                   ├─────────────────────┤
                                   │ • Turnstile Worker  │
                                   │ • Hold Sweeper (5s) │
                                   └──────────┬──────────┘
                                              │
                                              ▼
                                 ┌─────────────────────────┐
                                 │      PostgreSQL 16      │
                                 ├─────────────────────────┤
                                 │ • Client-side Pool (50) │
                                 │ • Partial B-Tree Index  │
                                 │ • SKIP LOCKED Row Locks │
                                 │ • Contiguous Window CTE │
                                 └─────────────────────────┘

```

```

## 2. Life of a Flash-Sale Request (Ingress Funnel)


```

┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 END-TO-END REQUEST LIFECYCLE                                      │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘

[ Client Request: POST /api/events//enter/ ]
│
▼
[ Sold-Out Circuit Breaker ] ──────────────► [ Tripped (0 seats) ] ──► HTTP 410 Gone (0.2ms)
│ (Seats Available)
▼
[ Anti-Bot Proof-of-Work ] ────────────────► [ Invalid Hash / Replay ] ──► HTTP 403 Forbidden
│ (Valid 65k-hash SHA-256)
▼
[ Redis Lua Token Bucket ]
│
┌──────────────┴──────────────┐
▼                             ▼
[ Token Available ]         [ Tokens Exhausted ]
(Direct Fast-Path)          (Congestion Burst)
│                             │
│                             ▼
│                 [ Enqueue into Redis ZSET ]
│                 (FIFO order via arrival score)
│                             │
│                             ▼
│                 [ Celery Turnstile Worker ]
│                 (Pops next batch of users)
│                             │
│                             ▼
│                 [ Channels WebSocket Push ]
│                 (Streams HMAC admission pass)
│                             │
└──────────────┬──────────────┘
│
▼
[ POST /api/events//allocate/ ]
(Guarded by X-Admission-Token)
│
▼
[ PostgreSQL Row-Level Allocation ]
(SELECT FOR UPDATE SKIP LOCKED)
│
▼
[ POST /api/reservations//confirm/ ]
(Guarded by Idempotency-Key)
│
▼
[ Booking Finalized: HELD -> BOOKED ]

```

## 3. Concurrency Mechanism: Standard Row Locking vs. SKIP LOCKED


```

┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│                       LOCK CONVOY ELIMINATION: HOW SKIP LOCKED WORKS                             │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘

## TRADITIONAL ROW LOCKING (Lock Convoy & Starvation)

Worker 1: ──► [ Lock Seat #1 ] ──(Holding lock for 25ms...)──────────────────► [ Success ]
Worker 2: ──► [ Requests Seat #1 ] ──► [ BLOCKED: Waiting on Worker 1... ] ──► [ Thread Blocked ]
Worker 3: ──► [ Requests Seat #1 ] ──► [ BLOCKED: Waiting on Worker 2... ] ──► [ Timeout / 500 ]

## POSTGRESQL SKIP LOCKED (Lock-Free Parallel Throughput)

Worker 1: ──► [ Lock Seat #1 ] ───────────────► Acquired Seat #1 immediately   (Latency: 2ms)
Worker 2: ──► [ Seat #1 Locked -> SKIPS ] ────► Acquired Seat #2 immediately   (Latency: 2ms)
Worker 3: ──► [ Seats #1, #2 Locked -> SKIPS ]► Acquired Seat #3 immediately   (Latency: 2ms)

```

## 4. Seat State Machine Diagram


```

┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   SEAT & RESERVATION LIFECYCLE                                    │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘

```
                            ┌─────────────────────────┐
                            │        AVAILABLE        │◄─────────────────────────┐
                            └────────────┬────────────┘                          │
                                         │                                       │
                           allocate_seats│ (SELECT FOR UPDATE                    │
                           with 300s TTL │  SKIP LOCKED)                         │
                                         ▼                                       │
                            ┌─────────────────────────┐                          │
                            │          HELD           │                          │
                            └──────┬────────────┬─────┘                          │
                                   │            │                                │
                   Payment Success │            │ TTL Expires                    │ User Cancels
                   within 300s TTL │            │ (held_until < NOW())           │ Booking
                                   ▼            ▼                                │
                    ┌──────────────┐    ┌──────────────┐                         │
                    │    BOOKED    │    │   EXPIRED    │                         │
                    │ (Reservation │    │ (Reclaimed by│                         │
                    │  CONFIRMED)  │    │ Celery Beat) │                         │
                    └──────┬───────┘    └───────┬──────┘                         │
                           │                    │                                │
                           │                    └────────────────────────────────┤
                           │                                                     │
                           └───────── User Requests Cancellation / Refund ───────┘

```

```

## 5. Contiguous Seating Allocation Mechanics


```

┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                CONTIGUOUS SEATING ALGORITHM (LEAD)                                │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘

Row B Physical Seats:  [Seat 1: Avail]  [Seat 2: Avail]  [Seat 3: Avail]  [Seat 4: Avail]
│
▼
PostgreSQL Window Query: LEAD(seat_number, 2 - 1) OVER (PARTITION BY section, row ORDER BY seat_number)

• Evaluates Seat 1: end_seat = 2. Span: (2 - 1) = 1 == (qty - 1) ──► Candidate Run [Seats 1, 2]
• Evaluates Seat 3: end_seat = 4. Span: (4 - 3) = 1 == (qty - 1) ──► Candidate Run [Seats 3, 4]

Handling Parallel Requests for 2 Adjacent Seats:
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. Worker 1 locks candidate run [Seats 1, 2] via SELECT ... FOR UPDATE SKIP LOCKED          │
│ 2. Worker 2 inspects [Seats 1, 2] -> Rows are locked -> SKIP LOCKED returns 0 rows          │
│ 3. Worker 2 automatically advances loop to evaluate next run [Seats 3, 4]                   │
│ 4. Worker 2 locks [Seats 3, 4] with zero wait time                                          │
│ 5. Outcome: Both workers complete in < 5ms with zero seat collisions or partial allocations │
└─────────────────────────────────────────────────────────────────────────────────────────────┘

```

## Architectural Highlights

* **Ingress Shielding**: Drops invalid or dead-end traffic at the Redis edge in under 0.2ms via an atomic sold-out circuit breaker and an asymmetric Proof-of-Work anti-bot puzzle (65,536 SHA-256 client trials verified in $O(1)$ time on the server).
* **Elimination of Lock Convoys**: Seat allocation uses PostgreSQL `SELECT FOR UPDATE SKIP LOCKED` and a partial B-Tree index on `status = 'AVAILABLE'`, allowing concurrent workers to bypass locked rows without thread blocking or deadlock cascades.
* **Contiguous Seat Grouping**: Employs a PostgreSQL window function (`LEAD`) Common Table Expression (CTE) to locate adjacent seats in the same row, automatically skipping locked subsets under high concurrency.
* **Asymmetric Virtual Waiting Room**: Buffers burst traffic into a Redis Sorted Set (`ZSET`) keyed by monotonic arrival timestamps. A Celery turnstile worker pops admitted batches and pushes HMAC-SHA256 admission tokens over Daphne WebSockets in real time.
* **Strict Idempotency**: Payment finalization is protected by database-backed idempotency keys and state-machine transitions (`AVAILABLE` $\to$ `HELD` $\to$ `BOOKED` / `EXPIRED` / `CANCELLED`), guaranteeing zero double-allocations or duplicate webhook charges under network retries.
* **Production Observability**: Features dual liveness/readiness health probes (`/healthz/live/`, `/healthz/ready/`), correlation-ID tracking middleware (`X-Request-ID`), and real-time Prometheus telemetry (`/metrics/`) exporting allocation latency histograms and queue depth gauges.

## Technology Stack

| Layer | Component | Purpose |
| :--- | :--- | :--- |
| **Runtime** | Python 3.12+ via `uv` | Dependency resolution and virtual environment |
| **Framework** | Django 5.1 (ASGI Mode) | Core HTTP routing and domain logic |
| **Database** | PostgreSQL 16 | ACID persistence with `psycopg3` connection pooling |
| **Cache & Queues** | Redis 7 | Token bucket rate limiting, ZSET waiting room, Channel layer |
| **WebSockets** | Daphne + Django Channels 4 | Duplex real-time queue position streaming |
| **Task Queue** | Celery 5.4+ & Celery Beat | Turnstile batch admissions and 5s hold sweeps |
| **Observability** | Prometheus Client | OpenMetrics exporter (`/metrics/`) |
| **Testing** | Locust, pytest-django | 38-test TDD suite and high-concurrency load testing |

## Project Structure

```text
ticketing_engine/
├── .env.example                        # Template for production and local environments
├── pyproject.toml                      # uv package definitions and pytest configuration
├── manage.py
├── config/                             # Application configuration root
│   ├── asgi.py                         # ASGI routing (HTTP + Django Channels WebSockets)
│   ├── celery.py                       # Celery worker and recurrent Beat schedule definitions
│   ├── settings.py                     # psycopg3 pooling, Redis caches, and channel layers
│   └── urls.py                         # Root URL configuration
├── tickets/                            # Core ticketing domain application
│   ├── models.py                       # Event, Seat, Reservation, PaymentRecord schemas
│   ├── services.py                     # Concurrency-safe ORM & raw SQL CTE allocation services
│   ├── exceptions.py                   # Domain exceptions (InsufficientSeatsAvailable, etc.)
│   ├── metrics.py                      # Centralized Prometheus metrics registry
│   ├── views.py                        # Ingress gate, checkout, and confirmation APIs
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

## Setup & Execution Guide

### 1. Prerequisites

* Python 3.12+
* `uv` package manager
* PostgreSQL 16 running on port 5432
* Redis 7 running on port 6379

### 2. Environment Configuration

Create `.env` at your project root:

```ini
DEBUG=False
SECRET_KEY=dev-entropy-secret-key-change-in-production!
ALLOWED_HOSTS=localhost,127.0.0.1,testserver

DB_NAME=ticket_engine_db
DB_USER=ticket_admin
DB_PASSWORD=secret_postgres_pass
DB_HOST=127.0.0.1
DB_PORT=5432

# psycopg3 native connection pool configuration
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

Provision the role and database in PostgreSQL via `psql`:

```sql
CREATE ROLE ticket_admin WITH LOGIN PASSWORD 'secret_postgres_pass' CREATEDB;
CREATE DATABASE ticket_engine_db OWNER ticket_admin ENCODING 'UTF8';
ALTER ROLE ticket_admin SET timezone TO 'UTC';
ALTER ROLE ticket_admin SET statement_timeout = '3000ms';
ALTER ROLE ticket_admin SET lock_timeout = '2000ms';

```

Apply migrations:

```powershell
uv run python manage.py migrate

```

### 4. Running the Test Suite

Execute the 38-test concurrency, security, and integration test suite:

```powershell
uv run python manage.py test tickets --keepdb

```

### 5. Running the Application Cluster

Start each process in a separate terminal:

```powershell
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

## High-Concurrency Load Benchmarking

Seed an event with 5,000 seats across 4 arena sections:

```powershell
uv run python manage.py seed_flash_sale --event-id 1 --seats 5000

```

Execute a headless Locust stress benchmark simulating 500 concurrent users hitting the full purchase funnel:

```powershell
uv run locust -f load_tests/locustfile.py --headless -u 500 -r 50 --run-time 60s --host [http://127.0.0.1:8000](http://127.0.0.1:8000)

```

Verify database integrity post-run to confirm zero over-allocation or seat double-booking:

```sql
SELECT status, count(*) FROM ticket_seats WHERE event_id = 1 GROUP BY status;

```

```

```