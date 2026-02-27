#!/usr/bin/env python3
"""
Demo script: submit a sample order through the full SAGA flow.
Run against LocalStack: python scripts/seed_data.py --local
Run against AWS:        python scripts/seed_data.py --endpoint https://your-api.execute-api.us-east-1.amazonaws.com/v1
"""
import argparse
import json
import time
import uuid

import requests

parser = argparse.ArgumentParser()
parser.add_argument("--endpoint", default="http://localhost:4566/restapis/local/v1/_user_request_")
parser.add_argument("--local", action="store_true")
args = parser.parse_args()

BASE_URL = "http://localhost:8000" if args.local else args.endpoint

order_payload = {
    "customer_id": f"cust-{uuid.uuid4().hex[:8]}",
    "items": [
        {"product_id": "prod-001", "quantity": 1, "unit_price_cents": 12999},
        {"product_id": "prod-002", "quantity": 2, "unit_price_cents": 4999},
    ],
}

idempotency_key = str(uuid.uuid4())

print(f"Submitting order with idempotency_key={idempotency_key}")
print(f"Payload: {json.dumps(order_payload, indent=2)}")
print()

resp = requests.post(
    f"{BASE_URL}/orders",
    json=order_payload,
    headers={
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
    },
    timeout=30,
)
print(f"Response [{resp.status_code}]: {json.dumps(resp.json(), indent=2)}")

if resp.status_code == 202:
    order_id = resp.json()["order_id"]
    print(f"\nOrder {order_id} submitted. Polling for status...")
    for attempt in range(10):
        time.sleep(2)
        status_resp = requests.get(f"{BASE_URL}/orders/{order_id}", timeout=10)
        status = status_resp.json().get("status")
        print(f"  Attempt {attempt + 1}: {status}")
        if status in ("CONFIRMED", "FAILED"):
            print(f"\nFinal status: {status}")
            if status == "CONFIRMED":
                print("Order processing complete!")
            else:
                print("Order failed. Check CloudWatch logs for details.")
            break
    else:
        print("Timed out waiting for order completion.")

print("\nDone. Submitting SAME request again to test idempotency...")
resp2 = requests.post(
    f"{BASE_URL}/orders",
    json=order_payload,
    headers={
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,  # Same key!
    },
    timeout=30,
)
resp2_data = resp2.json()
print(f"Response [{resp2.status_code}]: {json.dumps(resp2_data, indent=2)}")
assert resp2_data.get("order_id") == order_id, "Idempotency broken! Got different order_id"
print("Idempotency verified: same order_id returned for duplicate request.")
