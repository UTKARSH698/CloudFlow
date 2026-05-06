<div align="center">

# CloudFlow

### Distributed SAGA Orchestration on AWS Serverless

[![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![AWS CDK](https://img.shields.io/badge/AWS_CDK-IaC-FF9900?style=flat-square&logo=amazonaws&logoColor=white)](https://aws.amazon.com/cdk/)
[![Step Functions](https://img.shields.io/badge/Step_Functions-SAGA-FF4F8B?style=flat-square&logo=amazonaws&logoColor=white)](https://aws.amazon.com/step-functions/)
[![Tests](https://img.shields.io/badge/Tests-50%2B_passing-brightgreen?style=flat-square)](tests/)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

</div>

---

## What This Project Demonstrates

- **SAGA orchestration** — Step Functions coordinates a 4-step distributed transaction with explicit compensation paths. No 2PC, no distributed locks.
- - **Idempotency as a security primitive** — DynamoDB `attribute_not_exists` conditional writes deduplicate SQS at-least-once delivery and close replay-attack vectors simultaneously.
  - - **DynamoDB-backed circuit breaker** — circuit state shared across the entire Lambda fleet (cold-start safe). Fast-fails in < 1ms after N consecutive payment failures.
    - - **Event sourcing** — orders append immutable state transitions; full audit trail for time-travel debugging.
      - - **Measured performance** — load-tested at 1,100+ req/min, P50 = 47ms, P99 = 120ms on LocalStack. Bottlenecks documented.
        - - **50+ tests** — unit (moto mocks) + integration (LocalStack), covering every failure mode, compensation path, and idempotency edge case.
         
          - ---

          ## Architecture

          ```
          Client → API Gateway → Order Service λ
                                        │
                              ┌─────────▼──────────┐
                              │  Step Functions    │  SAGA Orchestrator
                              │  (state machine)   │
                              └──┬────────┬────────┘
                                 │        │
                        ┌────────▼┐   ┌───▼────────┐
                        │Inventory│   │  Payment   │ ← circuit breaker
                        │Service λ│   │  Service λ │   (DynamoDB-backed)
                        └────────┬┘   └───┬────────┘
                                 │        │
                        DynamoDB  │        │  DynamoDB
                        (atomic   │        │  (payments)
                         reserve) │        │
                                   ▼
                            ┌──────────────┐
                            │ Notification │ ← SQS → SNS
                            │  Service λ   │
                            └──────────────┘
                                   │
                              Dead Letter Queue → DLQ Processor λ → CloudWatch
          ```

          **Compensation path**: payment failure → Step Functions triggers inventory release → order marked COMPENSATED → customer notified. Every step is idempotent and checkpointed.

          ---

          ## Key Features

          | Feature | Implementation |
          |---|---|
          | SAGA orchestration | AWS Step Functions state machine with explicit happy + compensation paths |
          | Idempotency | DynamoDB `attribute_not_exists` — atomic check-and-set, per-service |
          | Circuit breaker | DynamoDB state store — shared across Lambda fleet, survives cold starts |
          | Event sourcing | Append-only order event log — `PENDING → CONFIRMED / COMPENSATED` |
          | Observability | Structured JSON logs, CloudWatch Metrics + Dashboards, X-Ray distributed traces |
          | Infrastructure as Code | 5 AWS CDK stacks — fully reproducible single-command deploy |
          | Local development | LocalStack via Docker — zero AWS cost for dev and CI |

          ---

          ## Tech Stack

          `Python 3.11` `AWS Step Functions` `AWS CDK` `DynamoDB` `SQS` `EventBridge` `SNS` `API Gateway` `Lambda` `CloudWatch` `X-Ray` `LocalStack` `pytest` `GitHub Actions`

          ---

          ## Distributed Systems Patterns

          **Why SAGA over 2PC?**
          2PC holds distributed locks across all participants — this kills throughput and leaves the system in limbo if the coordinator crashes. SAGA uses local transactions with explicit compensating transactions: each step is independently retryable, and Step Functions makes the entire flow visible as a single state machine diagram.

          **Why orchestration over choreography?**
          With money flows, implicit rollback is dangerous. Step Functions gives one place to read the compensation logic, one place to add retry policies, and one execution history to debug. Event choreography was used for non-critical downstream side effects via EventBridge.

          **CAP tradeoffs made explicit:**
          - Inventory writes: strong consistency (`ConditionalCheckFailed` catches concurrent conflicts)
          - - Order status reads: eventual consistency (polling is acceptable; staleness is not a correctness issue)
            - - Notifications: at-least-once (idempotent consumer handles duplicates safely)
             
              - ---

              ## Engineering Highlights

              **Idempotency as a dual-purpose primitive** — the same DynamoDB conditional write that prevents double-charging also closes the SQS replay-attack vector. One mechanism, two properties.

              **Circuit breaker state in DynamoDB** — Lambda instances are stateless and ephemeral. Storing circuit state in-process memory means each cold start resets the breaker, allowing the same provider failure to cascade across a fresh fleet. DynamoDB makes the state shared and persistent at +6ms per payment call — an acceptable tradeoff to prevent 30-second cascade timeouts.

              **Performance ceiling is LocalStack, not the design** — LocalStack DynamoDB writes take ~50ms vs ~8ms on real AWS. The ~1,300 req/min throughput ceiling is the Docker container's CPU budget. On real AWS DynamoDB, throughput scales horizontally with partition key distribution.

              ---

              ## Performance (LocalStack)

              | Metric | Result |
              |---|---|
              | Throughput | 1,100+ req/min at 10 threads |
              | P50 latency | 47ms |
              | P95 latency | 89ms |
              | P99 latency | 120ms |
              | Test suite | 50+ tests, all passing, < 30s |

              LocalStack is ~6x slower than real AWS DynamoDB. Correctness properties — idempotency, compensation, atomic writes — are identical.

              ---

              ## Quick Start

              **Prerequisites:** Python 3.11+, Docker Desktop, Node.js 18+ (for CDK)

              ```bash
              git clone https://github.com/UTKARSH698/CloudFlow
              cd CloudFlow

              # Linux / macOS
              make setup
              make test-unit     # 30+ unit tests, no Docker (~6s)
              make local-up      # Start LocalStack
              make test          # All 50+ tests

              # Windows
              .\run.ps1 setup
              .\run.ps1 test-unit
              .\run.ps1 local-up
              .\run.ps1 test
              ```

              **Deploy to AWS (optional):**
              ```bash
              make cdk-bootstrap  # first time only
              make deploy         # deploys all 5 CDK stacks
              make demo           # submit a sample order end-to-end
              ```

              ---

              ## Project Structure

              ```
              cloudflow/
              ├── infrastructure/       # AWS CDK (Python) — 5 stacks
              │   └── cloudflow/
              │       ├── api_stack.py
              │       ├── database_stack.py
              │       ├── messaging_stack.py
              │       ├── saga_stack.py
              │       └── monitoring_stack.py
              ├── services/
              │   ├── shared/           # idempotency, circuit_breaker, logger
              │   ├── order_service/
              │   ├── inventory_service/
              │   ├── payment_service/
              │   ├── notification_service/
              │   └── dlq_processor/
              ├── tests/
              │   ├── unit/             # moto mocks — pure logic, no Docker
              │   └── integration/      # LocalStack — real DynamoDB API
              ├── scripts/
              │   └── load_test.py
              ├── TECHNICAL.md          # Deep architecture: CAP analysis, tradeoffs, STRIDE, benchmarks
              └── .github/workflows/    # CI: test on PR, deploy on merge
              ```

              ---

              ## Deep Architecture

              For full technical depth — CAP theorem analysis, distributed transaction pattern comparison, STRIDE threat model, performance benchmark data, and the open formal question on compensation completeness — see **[TECHNICAL.md](TECHNICAL.md)**.

              ---

              ## Known Limitations

              - Compensation-state completeness is argued by test coverage, not formally proved
              - - Circuit breaker has a small concurrent-read race window (documented in TECHNICAL.md)
                - - LocalStack performance numbers are not production-representative (~6x slower)
                  - - Step Functions execution history expires after 90 days
                   
                    - ---

                    *MIT License · Utkarsh Batham*
