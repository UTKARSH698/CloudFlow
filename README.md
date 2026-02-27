# CloudFlow — Distributed Saga Orchestration on AWS

A **production-grade, serverless microservices platform** demonstrating distributed systems
fundamentals: the SAGA pattern, idempotency, event sourcing, circuit breakers, and distributed
tracing — all deployed on AWS using infrastructure-as-code.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          CLIENT                                     │
└────────────────────────────┬────────────────────────────────────────┘
                             │ POST /orders
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      API GATEWAY (REST)                             │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   ORDER SERVICE (Lambda)                            │
│  • Validates request                                                │
│  • Creates order (status: PENDING) in DynamoDB                     │
│  • Emits OrderCreated event → EventBridge                          │
│  • Starts Step Functions SAGA execution                             │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                SAGA ORCHESTRATOR (Step Functions)                   │
│                                                                     │
│  ┌─────────────┐   ┌──────────────┐   ┌─────────────────────┐     │
│  │  RESERVE    │──▶│    CHARGE    │──▶│   CONFIRM ORDER     │     │
│  │  INVENTORY  │   │   PAYMENT    │   │                     │     │
│  └──────┬──────┘   └──────┬───────┘   └─────────────────────┘     │
│         │ fail            │ fail                                    │
│         ▼                 ▼                                         │
│  ┌──────────────┐  ┌─────────────────────────────────┐            │
│  │ Mark Failed  │  │ RELEASE INVENTORY (compensate)  │            │
│  └──────────────┘  │ → Mark Failed                   │            │
│                    └─────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│              NOTIFICATION SERVICE (Lambda ← SQS)                   │
│  • Sends confirmation email / SMS via SNS                          │
└─────────────────────────────────────────────────────────────────────┘
```

### Services & AWS Resources

| Service | Lambda | Database | Queue |
|---|---|---|---|
| Order Service | `order-handler` | `orders` DynamoDB table | — |
| Inventory Service | `inventory-handler` | `inventory` DynamoDB table | `inventory-queue` SQS |
| Payment Service | `payment-handler` | `payments` DynamoDB table | `payment-queue` SQS |
| Notification Service | `notification-handler` | — | `notification-queue` SQS |

---

## Distributed Systems Concepts Demonstrated

### 1. SAGA Pattern (Choreography + Orchestration)
Long-running distributed transactions without 2PC. Each service owns its own database (no shared
state). Compensating transactions handle rollback when a step fails. Step Functions orchestrates
the happy path and compensation path explicitly.

### 2. Idempotency
Every Lambda handler checks a DynamoDB idempotency table before processing. Duplicate SQS
messages (at-least-once delivery) are safely deduplicated using the message's idempotency key.
This prevents double-charges and double-reservations without distributed locks.

### 3. Event Sourcing
Orders are never mutated in place. Every state transition (PENDING → CONFIRMED / FAILED) is
appended as an immutable event. The current state is derived by replaying events — enabling
full audit trails and time-travel debugging.

### 4. Circuit Breaker
The Payment Service wraps external payment provider calls in a circuit breaker backed by
DynamoDB. After N consecutive failures, the circuit opens and fast-fails requests, preventing
cascade failures across the system.

### 5. CAP Theorem Tradeoffs
- **Inventory reservations**: Strongly consistent reads (DynamoDB) — correctness matters here.
- **Order status queries**: Eventually consistent reads — acceptable staleness for read-heavy paths.
- **Notifications**: At-least-once delivery over exactly-once — idempotent consumers handle duplicates.

### 6. Distributed Tracing
AWS X-Ray traces propagate across Lambda ↔ SQS ↔ DynamoDB boundaries. Every request gets a
correlation ID that flows through all services and appears in CloudWatch Log Insights queries.

---

## Project Structure

```
cloudflow/
├── infrastructure/          # AWS CDK (Python) — all cloud resources as code
│   └── cloudflow/
│       ├── api_stack.py     # API Gateway + Order Service Lambda
│       ├── database_stack.py# DynamoDB tables
│       ├── messaging_stack.py# SQS queues + EventBridge bus
│       ├── saga_stack.py    # Step Functions state machine
│       └── monitoring_stack.py# CloudWatch dashboards + alarms
├── services/
│   ├── shared/              # Cross-cutting concerns
│   │   ├── events.py        # Pydantic event schemas
│   │   ├── idempotency.py   # Idempotency decorator
│   │   ├── circuit_breaker.py# Circuit breaker (DynamoDB-backed)
│   │   └── dynamodb.py      # DynamoDB helpers
│   ├── order_service/       # Create / query orders
│   ├── inventory_service/   # Reserve / release inventory
│   ├── payment_service/     # Charge / refund payments
│   └── notification_service/# Email / SMS notifications
├── tests/
│   ├── unit/                # Pure logic tests (no AWS)
│   └── integration/         # LocalStack-backed end-to-end tests
├── docker-compose.yml       # LocalStack for local development
├── Makefile                 # Common commands
└── .github/workflows/       # CI/CD pipeline
```

---

## Quick Start

### Prerequisites
- Python 3.11+
- Docker + Docker Compose
- Node.js 18+ (for AWS CDK)
- AWS CLI configured

### Local Development (LocalStack)

```bash
# Clone and set up
git clone https://github.com/UTKARSH698/CloudFlow
cd CloudFlow

# Linux / macOS
make setup
make local-up
make test-integration
make local-down

# Windows (PowerShell)
.\run.ps1 setup
.\run.ps1 local-up
.\run.ps1 test-integration
.\run.ps1 local-down
```

### Deploy to AWS

```bash
# Linux / macOS
make cdk-bootstrap   # First time only
make deploy
make demo

# Windows (PowerShell)
.\run.ps1 cdk-bootstrap
.\run.ps1 deploy
.\run.ps1 demo
```

### Running Tests

```bash
# Linux / macOS
make test-unit        # Unit tests only (fast, no Docker needed)
make test-integration # Integration tests against LocalStack
make test             # Both

# Windows (PowerShell)
.\run.ps1 test-unit
.\run.ps1 test-integration
.\run.ps1 test
```

---

## Key Design Decisions

**Why Step Functions for SAGA orchestration vs. pure choreography?**
Choreography (services reacting to each other's events) is simpler but makes compensation logic
implicit and hard to observe. Step Functions makes the entire saga—including compensation paths
—visible in a single state machine diagram. For a distributed transaction involving money, explicit
is safer than implicit.

**Why DynamoDB for idempotency vs. Redis?**
DynamoDB conditional writes (`attribute_not_exists(idempotency_key)`) provide exactly the
atomic check-and-set we need without a separate cache layer. It also survives Lambda cold starts
and scales with zero ops overhead.

**Why per-service DynamoDB tables vs. a shared database?**
Each service owning its data enforces the microservices boundary at the infrastructure level.
Services cannot bypass each other's APIs. Schema changes in one service don't break others.
This is the same reason Amazon itself moved away from shared Oracle databases.

---

## Observability

The CloudWatch dashboard tracks:
- Order throughput (orders/min)
- Saga success/failure rate
- P50/P99 Lambda duration per service
- Circuit breaker state changes
- DLQ message counts (failed events)

X-Ray service map shows the full call graph across Lambda → SQS → DynamoDB.

---

## References & Further Reading

- [SAGA pattern — Microservices.io](https://microservices.io/patterns/data/saga.html)
- [AWS Step Functions SAGA example](https://docs.aws.amazon.com/step-functions/latest/dg/sample-saga-lambda.html)
- [Designing Data-Intensive Applications — Kleppmann (Chapter 9: Consistency and Consensus)](https://dataintensive.net/)
- [AWS Well-Architected Framework — Reliability Pillar](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html)
