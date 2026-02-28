# CloudFlow — Distributed SAGA Order Processing on AWS

A **production-grade serverless microservices platform** demonstrating distributed systems
fundamentals: the SAGA pattern, idempotency, event sourcing, circuit breakers, and distributed
tracing — deployed on AWS using infrastructure-as-code.

> Built to showcase real distributed systems engineering: not just "it works", but *why* it works
> and what happens when it doesn't.

---

## Architecture

```mermaid
flowchart TD
    Client([Client]) -->|POST /orders| AG[API Gateway]
    AG --> OS[Order Service λ]

    OS -->|1 - Create order PENDING| ODB[(orders\nDynamoDB)]
    OS -->|2 - Publish OrderCreated| EB[EventBridge]
    OS -->|3 - Start SAGA execution| SF{Step Functions\nSAGA Orchestrator}

    SF -->|reserve| IS[Inventory Service λ]
    IS <-->|atomic conditional decrement| IDB[(inventory\nDynamoDB)]

    SF -->|charge| PS[Payment Service λ]
    PS -. circuit breaker .-> PP([External Payment\nProvider])
    PS --> PDB[(payments\nDynamoDB)]

    SF -->|confirm| OS
    OS -->|append CONFIRMED event| ODB

    SF -.->|COMPENSATE: release| IS
    IS -.->|restore stock| IDB
    SF -.->|COMPENSATE: notify| SQ[SQS Queue]
    SQ --> NS[Notification Service λ]
    NS --> SNS[SNS Topic]

    IS & PS <-->|deduplication| ID[(idempotency\nDynamoDB)]
    PS <-->|circuit state| CB[(circuit-breakers\nDynamoDB)]

    style SF fill:#FF9900,color:#000,stroke:#E68A00
    style ODB fill:#3F51B5,color:#fff,stroke:#303F9F
    style IDB fill:#3F51B5,color:#fff,stroke:#303F9F
    style PDB fill:#3F51B5,color:#fff,stroke:#303F9F
    style ID fill:#3F51B5,color:#fff,stroke:#303F9F
    style CB fill:#3F51B5,color:#fff,stroke:#303F9F
```

### Services & AWS Resources

| Service | Lambda | Database | Queue |
|---|---|---|---|
| Order Service | `order-handler` | `orders` DynamoDB (single-table) | — |
| Inventory Service | `inventory-handler` | `inventory` + `reservations` DynamoDB | — |
| Payment Service | `payment-handler` | `payments` DynamoDB | — |
| Notification Service | `notification-handler` | — | `notification-queue` SQS |

---

## Distributed Systems Concepts

### 1. SAGA Pattern (Orchestration)
Long-running distributed transactions without Two-Phase Commit. Each service owns its own
database. Step Functions orchestrates the happy path and compensation paths explicitly —
visible as a single state machine diagram. When payment fails, inventory is automatically
released via a compensating transaction.

### 2. Idempotency
Every Lambda handler uses a DynamoDB-backed idempotency decorator. SQS delivers messages
at-least-once — duplicates are deduplicated atomically using `attribute_not_exists` conditional
writes. No double-charges. No double-reservations. No distributed locks needed.

### 3. Event Sourcing
Orders are never updated in place. Every state transition (`PENDING → CONFIRMED → PAYMENT_CHARGED`)
is appended as an immutable event with a timestamp. Full audit trail. Time-travel debugging.
State is derived by replaying events.

### 4. Circuit Breaker (DynamoDB-backed)
The Payment Service wraps external provider calls in a circuit breaker stored in DynamoDB.
After N consecutive failures, the circuit opens — requests fast-fail in < 1ms instead of
waiting for a 30-second timeout. Because state lives in DynamoDB (not in-process memory),
all Lambda instances share the same circuit state across cold starts and concurrent invocations.

### 5. CAP Theorem Tradeoffs
- **Inventory reservations**: Strongly consistent reads — correctness matters, no overselling.
- **Order status queries**: Eventually consistent reads — acceptable staleness for read-heavy paths.
- **Notifications**: At-least-once delivery — idempotent consumers handle duplicates safely.

### 6. Distributed Tracing
AWS X-Ray traces propagate across Lambda ↔ SQS ↔ DynamoDB boundaries. Every request gets
a `correlation_id` that flows through all services and appears in CloudWatch Logs Insights.

---

## Failure Scenarios & Recovery

This is what separates a distributed system from a CRUD app. Every failure mode is handled explicitly.

| Scenario | What Happens | Guarantee Maintained |
|---|---|---|
| **Payment declined** | Step Functions triggers compensation: inventory released, customer notified | No stock held without payment |
| **Payment provider timeout** | Circuit breaker records failure; after 5 timeouts circuit opens; orders fast-fail | No cascade timeout across Lambda fleet |
| **Circuit breaker open** | Returns `PAYMENT_PROVIDER_UNAVAILABLE` with `retry_after_seconds` | No hanging requests |
| **Duplicate order request** | Idempotency table returns cached result; SAGA not started twice | Exactly-once order creation |
| **Inventory conflict** | DynamoDB `ConditionalCheckFailedException`; returns `INSUFFICIENT_STOCK` | No overselling; atomic at DB level |
| **SQS message redelivered** | Notification handler checks idempotency key; skips duplicate send | No double-emails |
| **Lambda crash mid-SAGA** | Step Functions retries the failed step from last checkpoint | No partial execution |
| **DynamoDB write fails** | Raises exception; Step Functions retries with backoff | At-least-once execution, idempotency handles duplicates |

### Failure Scenario Tests

```bash
# Run unit tests covering all failure scenarios
.\run.ps1 test-unit

# Specific failure test file
python -m pytest tests/unit/test_failure_scenarios.py -v
```

See `tests/unit/test_failure_scenarios.py` for:
- `test_payment_failure_triggers_compensation` — payment declines → inventory released
- `test_circuit_breaker_open_fast_fails` — open circuit rejects calls in < 1ms
- `test_duplicate_order_is_idempotent` — same order submitted twice, SAGA runs once
- `test_inventory_conflict_atomic` — concurrent reservations, one fails cleanly

---

## Observability

### Structured JSON Logging
Every service emits structured JSON logs (see `services/shared/logger.py`):
```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "service": "inventory_service.handler",
  "message": "Inventory reserved",
  "order_id": "abc-123",
  "reservation_id": "res-456",
  "correlation_id": "corr-789"
}
```
Query in CloudWatch Logs Insights:
```sql
fields @timestamp, order_id, level, message
| filter service = "inventory_service.handler" and level = "ERROR"
| sort @timestamp desc
| limit 20
```

### CloudWatch Metrics (via Monitoring Stack)
| Metric | Alarm Threshold |
|---|---|
| `OrdersCreated` | — |
| `SAGASuccessRate` | < 95% for 5 minutes |
| `SAGACompensationRate` | > 10% triggers alert |
| `LambdaDuration p99` | > 3000ms |
| `CircuitBreakerStateChange` | Any OPEN transition |
| `DLQMessageCount` | > 0 (no messages should reach DLQ) |

### Load Test Results
```
Test: 50 concurrent order requests
Tool: scripts/load_test.py (Python ThreadPoolExecutor)

Results (LocalStack):
  Total requests:     50
  Successful:         50 (100%)
  Idempotency hits:   0  (all unique orders)
  Avg latency:        ~45ms (DynamoDB conditional write)
  P99 latency:        ~120ms
  Throughput:         ~1,100 req/min

Note: Real AWS DynamoDB is 3-5x faster than LocalStack.
```
Run: `python scripts/load_test.py --orders 50 --concurrency 10`

---

## Key Design Decisions & Tradeoffs

### Why SAGA instead of Two-Phase Commit (2PC)?

**2PC problems:**
- All participants hold locks during the protocol → throughput collapses under load
- If the coordinator crashes between Phase 1 and Phase 2 → system stuck in limbo forever
- Requires all services to implement a `prepare` protocol — tightly coupled
- AWS Lambda has no persistent connections → coordinator can't hold distributed locks

**SAGA advantages:**
- Each step is a local transaction — no distributed locks
- Compensation is business logic, not database magic → testable, readable
- Step Functions makes the entire saga visible in one diagram
- Partial failures have explicit, known recovery paths

### Why Orchestration (Step Functions) instead of Choreography (events)?

**Choreography:** Services react to each other's events. Inventory hears `OrderCreated`,
publishes `StockReserved`. Payment hears `StockReserved`, publishes `PaymentCharged`.

**Why we didn't:**
- Compensation is implicit — each service must listen for failure events from every other service
- Debugging requires correlating logs across 4 services to reconstruct what happened
- Adding a new step requires modifying multiple services
- When money is involved, implicit rollback is dangerous

**Orchestration (our approach):**
- One state machine shows the entire flow
- Compensation paths are explicit and version-controlled
- One place to add retry policies, timeouts, and error handling

### Why DynamoDB for idempotency instead of Redis?

- DynamoDB `attribute_not_exists` is an **atomic** check-and-set at the database level
- Redis requires Lua scripts for atomic check-and-set (more complexity)
- DynamoDB survives Lambda cold starts — Redis connections can be stale
- No extra infrastructure: DynamoDB is already the primary datastore
- Scales infinitely with zero ops overhead

### Why per-service DynamoDB tables instead of a shared database?

Each service owns its data. This enforces the microservices boundary at the infrastructure level:
- Services cannot bypass each other's APIs to read/write data
- Schema changes in one service don't break others
- Each table can be tuned independently (read/write capacity, GSIs)
- This is why Amazon itself moved from a shared Oracle database to service-owned data

### CAP Theorem Positioning

CloudFlow prioritizes **availability over strict consistency** for reads, with **strong consistency**
only where correctness is critical:

```
Inventory writes:  Strong consistency  (ConditionalCheckFailed catches conflicts)
Inventory reads:   Strong consistency  (prevents overselling)
Order reads:       Eventual consistency (acceptable: user polling for status)
Notifications:     At-least-once       (idempotent consumer handles duplicates)
```

This matches how large-scale systems (Amazon, Netflix) handle the CAP tradeoff: strong
consistency only where business rules require it, eventual consistency everywhere else.

---

## Security

### Input Validation
All requests are validated via Pydantic schemas before any database operation:
```python
class CreateOrderRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    items: list[OrderItem] = Field(min_length=1)
    # Negative quantities, zero-price items, empty orders → rejected at validation
```

### IAM Least Privilege
Each Lambda has its own IAM role with only the permissions it needs:
- `order-handler`: `dynamodb:PutItem` on `orders` table, `events:PutEvents`, `states:StartExecution`
- `inventory-handler`: `dynamodb:UpdateItem` on `inventory`, `dynamodb:PutItem` on `reservations`
- `payment-handler`: `dynamodb:PutItem` on `payments`, `dynamodb:GetItem` on `payments`
- No Lambda has admin permissions or cross-service database access

### No Secrets in Code
- Payment provider credentials: fetched from **AWS Secrets Manager** at runtime (not env vars)
- DynamoDB table names: passed via environment variables set by CDK at deploy time
- No hardcoded credentials anywhere — verified by pre-commit scan

### API Gateway Protections
- **Request validation**: API Gateway rejects malformed JSON before Lambda is invoked
- **Rate limiting**: Usage plans limit requests per second per API key
- **CORS**: Configured to allow only trusted origins

### Data Integrity
- All financial amounts stored as **integer cents** (no floating point)
- DynamoDB optimistic locking with version counters prevents lost updates
- Idempotency keys expire after 24 hours (TTL on idempotency table)

---

## Project Structure

```
cloudflow/
├── infrastructure/              # AWS CDK (Python) — all cloud resources as code
│   └── cloudflow/
│       ├── api_stack.py         # API Gateway + Order Service Lambda
│       ├── database_stack.py    # DynamoDB tables (5 tables)
│       ├── messaging_stack.py   # SQS queues + EventBridge bus
│       ├── saga_stack.py        # Step Functions state machine (SAGA)
│       └── monitoring_stack.py  # CloudWatch dashboards + alarms
├── services/
│   ├── shared/                  # Cross-cutting concerns
│   │   ├── events.py            # Pydantic event schemas
│   │   ├── idempotency.py       # @idempotent decorator (DynamoDB-backed)
│   │   ├── circuit_breaker.py   # Circuit breaker (DynamoDB-backed)
│   │   ├── dynamodb.py          # DynamoDB helpers
│   │   └── logger.py            # Structured JSON logging
│   ├── order_service/           # Create orders, manage event sourcing log
│   ├── inventory_service/       # Atomic reserve / release (SAGA steps)
│   ├── payment_service/         # Charge / refund with circuit breaker
│   └── notification_service/    # SQS-triggered email/SMS notifications
├── tests/
│   ├── unit/                    # moto mocks — pure logic, no Docker
│   │   ├── test_circuit_breaker.py
│   │   ├── test_idempotency.py
│   │   ├── test_events.py
│   │   └── test_failure_scenarios.py   # failure mode coverage
│   └── integration/             # LocalStack — real DynamoDB via Docker
│       └── test_saga_flow.py
├── scripts/
│   ├── load_test.py             # Concurrent load test with latency metrics
│   └── seed_data.py             # Seed demo data into LocalStack
├── docker-compose.yml           # LocalStack for local development
├── run.ps1                      # Windows PowerShell commands
├── Makefile                     # Linux/macOS commands
├── TECHNICAL.md                 # Deep-dive: architecture decisions & analysis
└── .github/workflows/           # CI/CD: test on PR, deploy on merge to main
```

---

## Quick Start

### Prerequisites
- Python 3.11+
- Docker Desktop (for LocalStack integration tests)
- Node.js 18+ (for AWS CDK)

### Install & Run Tests

```bash
# Clone
git clone https://github.com/UTKARSH698/CloudFlow
cd CloudFlow

# Linux / macOS
make setup
make test-unit          # 15+ unit tests, no Docker needed (~6s)
make local-up           # Start LocalStack
make test               # All 22+ tests
make local-down

# Windows (PowerShell)
.\run.ps1 setup
.\run.ps1 test-unit
.\run.ps1 local-up
.\run.ps1 test
.\run.ps1 local-down
```

### Deploy to AWS (optional)
```bash
# Configure AWS credentials first (aws configure)
make cdk-bootstrap      # First time only
make deploy             # Deploys all 5 stacks
make demo               # Submit a sample order end-to-end
```

---

## References & Further Reading

- [SAGA Pattern — Microservices.io](https://microservices.io/patterns/data/saga.html)
- [AWS Step Functions SAGA example](https://docs.aws.amazon.com/step-functions/latest/dg/sample-saga-lambda.html)
- [Designing Data-Intensive Applications — Kleppmann (Chapter 9)](https://dataintensive.net/)
- [AWS Well-Architected Framework — Reliability Pillar](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html)
- [DynamoDB Single-Table Design — Rick Houlihan](https://www.youtube.com/watch?v=BnDKD_Zv0og)
- [Circuit Breaker Pattern — Martin Fowler](https://martinfowler.com/bliki/CircuitBreaker.html)
