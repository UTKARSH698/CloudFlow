.PHONY: help setup local-up local-down test test-unit test-integration \
        cdk-bootstrap deploy logs demo clean

PYTHON    = python3
PIP       = pip3
AWS       = aws --endpoint-url=http://localhost:4566 --region us-east-1 \
               --no-cli-pager

help:
	@echo ""
	@echo "CloudFlow — Makefile Commands"
	@echo "────────────────────────────────────────────"
	@echo "  make setup            Install all dependencies"
	@echo "  make local-up         Start LocalStack (Docker)"
	@echo "  make local-down       Stop LocalStack"
	@echo "  make test             Run all tests"
	@echo "  make test-unit        Unit tests only (no Docker)"
	@echo "  make test-integration Integration tests (needs LocalStack)"
	@echo "  make cdk-bootstrap    Bootstrap CDK (run once per account)"
	@echo "  make deploy           Deploy all stacks to AWS"
	@echo "  make logs             Tail logs from all services"
	@echo "  make demo             Submit a sample order end-to-end"
	@echo "  make clean            Remove build artifacts"
	@echo ""

# ──────────────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────────────
setup:
	$(PIP) install -r requirements-dev.txt
	cd infrastructure && $(PIP) install -r requirements.txt
	npm install -g aws-cdk
	@echo "Setup complete."

# ──────────────────────────────────────────────────────────────────────────────
# Local development
# ──────────────────────────────────────────────────────────────────────────────
local-up:
	docker compose up -d --wait
	@echo ""
	@echo "LocalStack is running at http://localhost:4566"
	@echo "Run 'make test-integration' to verify."

local-down:
	docker compose down

# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────
test: test-unit test-integration

test-unit:
	@echo "Running unit tests..."
	PYTHONPATH=services pytest tests/unit/ -v --tb=short -q

test-integration: local-up
	@echo "Running integration tests..."
	USE_LOCALSTACK=true LOCALSTACK_ENDPOINT=http://localhost:4566 \
	PYTHONPATH=services pytest tests/integration/ -v --tb=short -m integration

# ──────────────────────────────────────────────────────────────────────────────
# AWS deployment
# ──────────────────────────────────────────────────────────────────────────────
cdk-bootstrap:
	cd infrastructure && cdk bootstrap

deploy:
	cd infrastructure && cdk deploy --all --require-approval never

diff:
	cd infrastructure && cdk diff --all

# ──────────────────────────────────────────────────────────────────────────────
# Operations
# ──────────────────────────────────────────────────────────────────────────────
logs:
	@echo "Tailing logs for all CloudFlow Lambda functions..."
	aws logs tail /aws/lambda/cloudflow-order-service --follow &
	aws logs tail /aws/lambda/cloudflow-inventory-service --follow &
	aws logs tail /aws/lambda/cloudflow-payment-service --follow &
	aws logs tail /aws/lambda/cloudflow-notification-service --follow

demo:
	@echo "Submitting a demo order..."
	PYTHONPATH=services $(PYTHON) scripts/seed_data.py --local

# List DLQ messages (check for poison pills)
check-dlq:
	@for queue in notification inventory payment; do \
		echo "=== $$queue DLQ ==="; \
		$(AWS) sqs get-queue-attributes \
			--queue-url http://localhost:4566/000000000000/cloudflow-$$queue-dlq \
			--attribute-names ApproximateNumberOfMessages; \
	done

# ──────────────────────────────────────────────────────────────────────────────
# Housekeeping
# ──────────────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -f coverage.xml .coverage
	@echo "Cleaned."
