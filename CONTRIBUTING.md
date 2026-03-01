# Contributing to CloudFlow

Thanks for your interest in contributing! This guide covers everything you need to get started.

---

## Getting Started

### Prerequisites
- Python 3.11+
- Docker Desktop (for LocalStack)
- AWS CDK CLI (`npm install -g aws-cdk`)

### Setup

```powershell
# Windows
.\run.ps1 setup       # install dependencies
.\run.ps1 local-up    # start LocalStack
.\run.ps1 test        # run all tests
```

```bash
# Linux / macOS
make setup
make local-up
make test
```

---

## Project Structure

```
services/        # Lambda handlers (one folder per microservice)
  shared/        # Utilities shared across all services
infrastructure/  # AWS CDK stacks (IaC)
tests/
  unit/          # Fast tests using moto (no Docker needed)
  integration/   # Full flow tests against LocalStack
scripts/         # Load testing + demo data seeding
```

---

## Making Changes

### Adding a new feature
1. Add business logic in the relevant `services/` folder
2. Add unit tests in `tests/unit/`
3. Run `pytest tests/unit/` to verify

### Adding a new service
1. Create `services/<service_name>/handler.py`
2. Add a Lambda function in `infrastructure/cloudflow/api_stack.py`
3. Grant the necessary DynamoDB/SQS permissions in the stack
4. Add tests in `tests/unit/` and `tests/integration/`

---

## Code Standards

- **Formatter:** `ruff format services/ tests/`
- **Linter:** `ruff check services/ tests/`
- **Type checker:** `mypy services/shared/ --ignore-missing-imports`
- **Test coverage:** must stay above 70% (`--cov-fail-under=70`)

Run all checks before submitting:
```bash
ruff check services/ tests/ && mypy services/shared/ --ignore-missing-imports && pytest tests/unit/
```

---

## Commit Message Format

```
Short summary (max 72 chars)

Optional longer description explaining the why, not the what.
```

Examples:
- `Fix circuit breaker not resetting after timeout`
- `Add payment retry logic with exponential backoff`
- `Update README with new architecture diagram`

---

## Pull Request Checklist

- [ ] Tests pass (`pytest tests/unit/`)
- [ ] No linting errors (`ruff check services/ tests/`)
- [ ] Coverage stays above 70%
- [ ] Relevant documentation updated (README / TECHNICAL.md)

---

## Reporting Issues

Open an issue on [GitHub](https://github.com/UTKARSH698/CloudFlow/issues) with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- LocalStack / Python version
