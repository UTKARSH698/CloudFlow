"""
Payment Service Lambda Handler
================================
Wraps an external payment provider (Stripe-compatible mock) with:
  1. Idempotency — no double charges
  2. Circuit breaker — stops cascade failures when provider is down
  3. Compensating transaction (refund) for SAGA rollback

Why the circuit breaker lives in the payment service specifically:
  External payment APIs are the most common source of cascade failures.
  When Stripe has an incident, every order attempt times out at 30s.
  With circuit breaker: after 5 timeouts, the circuit opens and orders
  fail-fast (< 10ms), freeing Lambda concurrency for other traffic.
  Without it: 100 concurrent orders × 30s timeout = Lambda concurrency exhausted.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder

from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from shared.dynamodb import get_table
from shared.idempotency import idempotent

patch_all()

from shared.logger import get_logger
logger = get_logger(__name__)

PAYMENTS_TABLE = os.environ.get("PAYMENTS_TABLE", "cloudflow-payments")
PAYMENT_PROVIDER_URL = os.environ.get("PAYMENT_PROVIDER_URL", "https://api.payment-mock.internal")

payments_table = get_table(PAYMENTS_TABLE)

# Shared circuit breaker instance — state lives in DynamoDB, not in-process
payment_circuit_breaker = CircuitBreaker(
    name="external-payment-provider",
    failure_threshold=5,
    success_threshold=2,
    timeout_seconds=60,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:
    action = event.get("action")
    try:
        if action == "charge":
            return _charge(event)
        if action == "refund":
            return _refund(event)
        raise ValueError(f"Unknown action: {action!r}")
    except CircuitBreakerOpenError as e:
        logger.warning("Payment circuit breaker OPEN: %s", e)
        return {
            "success": False,
            "error": "PAYMENT_PROVIDER_UNAVAILABLE",
            "message": "Payment service temporarily unavailable. Please retry shortly.",
            "retry_after_seconds": int(e.resets_at - __import__("time").time()),
        }
    except PaymentDeclinedError as e:
        logger.info("Payment declined for order %s: %s", event.get("order_id"), e)
        return {"success": False, "error": "PAYMENT_DECLINED", "message": str(e)}
    except Exception:
        logger.exception("Payment service error")
        raise


# ---------------------------------------------------------------------------
# Charge
# ---------------------------------------------------------------------------

def _charge(event: dict) -> dict:
    order_id = event["order_id"]
    customer_id = event["customer_id"]
    amount_cents = event["total_cents"]
    payment_id = str(uuid.uuid4())

    @idempotent(key_fn=lambda: f"charge-{order_id}")
    def _do_charge():
        with xray_recorder.in_subsegment("payment_charge"):
            # Call through circuit breaker — this is the external API call
            provider_response = payment_circuit_breaker.call(
                _call_payment_provider,
                action="charge",
                customer_id=customer_id,
                amount_cents=amount_cents,
                idempotency_key=f"charge-{order_id}",
            )

            if not provider_response.get("success"):
                raise PaymentDeclinedError(provider_response.get("decline_reason", "Card declined"))

            payments_table.put_item(Item={
                "payment_id": payment_id,
                "order_id": order_id,
                "customer_id": customer_id,
                "amount_cents": amount_cents,
                "provider_charge_id": provider_response["charge_id"],
                "status": "CHARGED",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

            logger.info(
                "Payment charged",
                extra={
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "amount_cents": amount_cents,
                },
            )
            return {"success": True, "payment_id": payment_id}

    return _do_charge()


# ---------------------------------------------------------------------------
# Refund (compensating transaction)
# ---------------------------------------------------------------------------

def _refund(event: dict) -> dict:
    order_id = event["order_id"]
    payment_id = event.get("payment_id")

    if not payment_id:
        logger.info("No payment_id for order %s — nothing to refund", order_id)
        return {"success": True, "message": "Nothing to refund"}

    @idempotent(key_fn=lambda: f"refund-{payment_id}")
    def _do_refund():
        with xray_recorder.in_subsegment("payment_refund"):
            # Look up the original charge
            resp = payments_table.get_item(Key={"payment_id": payment_id})
            payment = resp.get("Item")
            if not payment:
                logger.warning("Payment %s not found — nothing to refund", payment_id)
                return {"success": True, "message": "Nothing to refund"}

            provider_charge_id = payment["provider_charge_id"]

            payment_circuit_breaker.call(
                _call_payment_provider,
                action="refund",
                charge_id=provider_charge_id,
                idempotency_key=f"refund-{payment_id}",
            )

            payments_table.update_item(
                Key={"payment_id": payment_id},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "REFUNDED"},
            )

            logger.info("Payment refunded", extra={"payment_id": payment_id, "order_id": order_id})
            return {"success": True}

    return _do_refund()


# ---------------------------------------------------------------------------
# External provider mock (replace with real Stripe/Braintree SDK in prod)
# ---------------------------------------------------------------------------

def _call_payment_provider(action: str, **kwargs) -> dict:
    """
    Simulates an external payment API call.

    In production: replace with boto3 call to Secrets Manager for API keys,
    then call Stripe SDK or requests to payment gateway.

    Raises an exception on network timeout (simulated) so the circuit breaker
    can record the failure.
    """
    import random
    import time

    # Simulate realistic latency
    time.sleep(random.uniform(0.05, 0.15))

    # Simulate a 3% failure rate for circuit breaker demo purposes
    if random.random() < 0.03:
        raise ConnectionError("Payment provider timed out")

    if action == "charge":
        return {
            "success": True,
            "charge_id": f"ch_{uuid.uuid4().hex[:16]}",
        }
    if action == "refund":
        return {"success": True}
    raise ValueError(f"Unknown provider action: {action!r}")


class PaymentDeclinedError(Exception):
    pass
