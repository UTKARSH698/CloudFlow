#!/usr/bin/env bash
# local_setup.sh
# Provisions all AWS resources in LocalStack for local development.
# Called automatically by docker-compose setup service.

set -euo pipefail

ENDPOINT="http://localstack:4566"
REGION="us-east-1"

echo "==> Creating DynamoDB tables..."

create_table() {
  local name=$1
  local pk=$2
  local pk_type=${3:-S}
  local sk=${4:-}
  local sk_type=${5:-S}

  if [ -n "$sk" ]; then
    aws --endpoint-url="$ENDPOINT" --region="$REGION" dynamodb create-table \
      --table-name "$name" \
      --attribute-definitions \
        AttributeName="$pk",AttributeType="$pk_type" \
        AttributeName="$sk",AttributeType="$sk_type" \
      --key-schema \
        AttributeName="$pk",KeyType=HASH \
        AttributeName="$sk",KeyType=RANGE \
      --billing-mode PAY_PER_REQUEST \
      --no-cli-pager 2>/dev/null || echo "  Table $name already exists"
  else
    aws --endpoint-url="$ENDPOINT" --region="$REGION" dynamodb create-table \
      --table-name "$name" \
      --attribute-definitions AttributeName="$pk",AttributeType="$pk_type" \
      --key-schema AttributeName="$pk",KeyType=HASH \
      --billing-mode PAY_PER_REQUEST \
      --no-cli-pager 2>/dev/null || echo "  Table $name already exists"
  fi
  echo "  [OK] $name"
}

create_table "cloudflow-orders"         "pk" "S" "sk" "S"
create_table "cloudflow-inventory"      "product_id"
create_table "cloudflow-reservations"   "reservation_id"
create_table "cloudflow-payments"       "payment_id"
create_table "cloudflow-idempotency"    "idempotency_key"
create_table "cloudflow-circuit-breakers" "name"

echo ""
echo "==> Seeding inventory data..."

seed_product() {
  local product_id=$1
  local name=$2
  local quantity=$3
  local price=$4

  aws --endpoint-url="$ENDPOINT" --region="$REGION" dynamodb put-item \
    --table-name "cloudflow-inventory" \
    --item "{
      \"product_id\": {\"S\": \"$product_id\"},
      \"name\": {\"S\": \"$name\"},
      \"quantity\": {\"N\": \"$quantity\"},
      \"unit_price_cents\": {\"N\": \"$price\"},
      \"updated_at\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
    }" \
    --no-cli-pager
  echo "  [OK] $name (qty=$quantity)"
}

seed_product "prod-001" "Mechanical Keyboard"     50  12999
seed_product "prod-002" "USB-C Hub"               100  4999
seed_product "prod-003" "27-inch Monitor"         20  29999
seed_product "prod-004" "Wireless Mouse"          75   3999
seed_product "prod-005" "Webcam 4K"               1   14999  # Limited stock for testing

echo ""
echo "==> Creating SQS queues..."

create_queue() {
  local name=$1
  aws --endpoint-url="$ENDPOINT" --region="$REGION" sqs create-queue \
    --queue-name "$name" \
    --no-cli-pager 2>/dev/null || true
  echo "  [OK] $name"
}

create_queue "cloudflow-notification"
create_queue "cloudflow-notification-dlq"
create_queue "cloudflow-inventory"
create_queue "cloudflow-inventory-dlq"
create_queue "cloudflow-payment"
create_queue "cloudflow-payment-dlq"

echo ""
echo "==> Creating EventBridge bus..."
aws --endpoint-url="$ENDPOINT" --region="$REGION" events create-event-bus \
  --name "cloudflow-events" \
  --no-cli-pager 2>/dev/null || echo "  Bus already exists"
echo "  [OK] cloudflow-events"

echo ""
echo "============================================"
echo " LocalStack setup complete!"
echo " All CloudFlow resources are ready."
echo " Run: make test-integration"
echo "============================================"
