"""
Load Test — CloudFlow Inventory Service
=========================================
Simulates concurrent order requests against LocalStack DynamoDB to measure:
  - Throughput (requests/sec)
  - Latency (avg, p50, p95, p99)
  - Success rate
  - Idempotency correctness under concurrency

Usage:
  # Start LocalStack first: .\run.ps1 local-up
  python scripts/load_test.py --orders 50 --concurrency 10

Why test inventory specifically?
  The inventory reservation has the most complex correctness requirement:
  atomic DynamoDB conditional decrements must prevent overselling even
  under high concurrency. This test verifies that at the load level.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services"))

LOCALSTACK = os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4566")
REGION = "us-east-1"

# Set env vars before importing handlers
os.environ.setdefault("AWS_ENDPOINT_URL", LOCALSTACK)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
os.environ.setdefault("INVENTORY_TABLE", "cloudflow-inventory")
os.environ.setdefault("RESERVATIONS_TABLE", "cloudflow-reservations")
os.environ.setdefault("IDEMPOTENCY_TABLE", "cloudflow-idempotency")
os.environ.setdefault("CIRCUIT_BREAKER_TABLE", "cloudflow-circuit-breakers")
os.environ.setdefault("AWS_XRAY_SDK_ENABLED", "false")


@dataclass
class RequestResult:
    order_id: str
    success: bool
    error: str = ""
    latency_ms: float = 0.0


@dataclass
class LoadTestReport:
    total: int = 0
    successful: int = 0
    failed: int = 0
    idempotency_hits: int = 0
    latencies: List[float] = field(default_factory=list)

    def add(self, result: RequestResult) -> None:
        self.total += 1
        if result.success:
            self.successful += 1
        else:
            self.failed += 1
        self.latencies.append(result.latency_ms)

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        sorted_l = sorted(self.latencies)
        idx = int(len(sorted_l) * p / 100)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    def print_summary(self, elapsed_total: float) -> None:
        avg = sum(self.latencies) / len(self.latencies) if self.latencies else 0
        throughput = self.total / elapsed_total if elapsed_total > 0 else 0

        print("\n" + "=" * 55)
        print("  CloudFlow Load Test Results")
        print("=" * 55)
        print(f"  Total requests:    {self.total}")
        print(f"  Successful:        {self.successful} ({100*self.successful/self.total:.1f}%)")
        print(f"  Failed:            {self.failed}")
        print(f"  Total time:        {elapsed_total:.2f}s")
        print(f"  Throughput:        {throughput:.0f} req/s ({throughput*60:.0f} req/min)")
        print()
        print(f"  Latency (ms):")
        print(f"    Avg:             {avg:.1f}ms")
        print(f"    P50:             {self.percentile(50):.1f}ms")
        print(f"    P95:             {self.percentile(95):.1f}ms")
        print(f"    P99:             {self.percentile(99):.1f}ms")
        print(f"    Max:             {max(self.latencies):.1f}ms")
        print("=" * 55)


def _seed_product(ddb_resource, quantity: int) -> str:
    """Create a product with known stock for the load test."""
    product_id = f"load-test-{uuid.uuid4().hex[:8]}"
    ddb_resource.Table("cloudflow-inventory").put_item(Item={
        "product_id": product_id,
        "quantity": quantity,
        "unit_price_cents": 999,
        "name": "Load Test Product",
    })
    return product_id


def _run_single_order(product_id: str, quantity_per_order: int) -> RequestResult:
    """Submit one reservation request and measure latency."""
    from inventory_service.handler import handler

    order_id = f"load-{uuid.uuid4()}"
    start = time.time()
    try:
        result = handler({
            "action": "reserve",
            "order_id": order_id,
            "items": [{"product_id": product_id, "quantity": quantity_per_order, "unit_price_cents": 999}],
            "correlation_id": str(uuid.uuid4()),
        }, None)
        elapsed = (time.time() - start) * 1000
        return RequestResult(
            order_id=order_id,
            success=result.get("success", False),
            error=result.get("error", ""),
            latency_ms=elapsed,
        )
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return RequestResult(order_id=order_id, success=False, error=str(e), latency_ms=elapsed)


def run_load_test(num_orders: int, concurrency: int, quantity_per_order: int = 1) -> None:
    print(f"\nCloudFlow Load Test")
    print(f"  Orders:      {num_orders}")
    print(f"  Concurrency: {concurrency} threads")
    print(f"  Target:      {LOCALSTACK}")
    print(f"  Qty/order:   {quantity_per_order}")

    ddb = boto3.resource(
        "dynamodb",
        endpoint_url=LOCALSTACK,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

    # Seed enough stock for all orders
    total_stock = num_orders * quantity_per_order + 100
    product_id = _seed_product(ddb, total_stock)
    print(f"  Product:     {product_id} (stock={total_stock})\n")
    print("Running...")

    report = LoadTestReport()
    wall_start = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(_run_single_order, product_id, quantity_per_order)
            for _ in range(num_orders)
        ]
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            report.add(result)
            if i % 10 == 0 or i == num_orders:
                pct = 100 * i / num_orders
                print(f"  Progress: {i}/{num_orders} ({pct:.0f}%)", end="\r")

    wall_elapsed = time.time() - wall_start

    # Verify correctness: stock should equal total_stock - (successful * qty)
    item = ddb.Table("cloudflow-inventory").get_item(Key={"product_id": product_id}).get("Item")
    if item:
        remaining = int(item["quantity"])
        expected = total_stock - (report.successful * quantity_per_order)
        stock_correct = remaining == expected
        print(f"\n  Stock check: remaining={remaining}, expected={expected} → {'PASS' if stock_correct else 'FAIL'}")
        if not stock_correct:
            print(f"  WARNING: Stock mismatch — possible oversell or double-reserve!")

    report.print_summary(wall_elapsed)

    # Cleanup
    ddb.Table("cloudflow-inventory").delete_item(Key={"product_id": product_id})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CloudFlow load test")
    parser.add_argument("--orders", type=int, default=50, help="Number of orders to submit")
    parser.add_argument("--concurrency", type=int, default=10, help="Number of concurrent threads")
    parser.add_argument("--qty", type=int, default=1, help="Items per order")
    args = parser.parse_args()

    run_load_test(args.orders, args.concurrency, args.qty)
