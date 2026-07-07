"""
Circuit Breaker — Preventing Cascade Failures
=============================================
When the payment provider is down, we should stop hammering it immediately
rather than letting every order attempt timeout after 30 seconds. The circuit
breaker pattern (Fowler, 2014) is the standard solution.

States:
  CLOSED   → normal operation, requests pass through
  OPEN     → failure threshold exceeded, requests fast-fail immediately
  HALF_OPEN → cooldown elapsed, one probe request allowed through

Why DynamoDB-backed vs in-process?
  Lambda functions are ephemeral and short-lived. An in-process circuit breaker
  resets on every cold start. A DynamoDB-backed breaker shares state across
  ALL Lambda instances and survives restarts — which is what we actually want.

Tradeoffs vs ElastiCache Redis:
  + DynamoDB: no VPC required, no cluster management, strongly consistent
  - DynamoDB: higher latency (~single digit ms vs sub-ms Redis), higher cost at scale
  For a payment circuit breaker where we make ~1 call per order, DynamoDB wins.
"""
from __future__ import annotations

import logging
import os
import time
from enum import Enum
from typing import Any, Callable, TypeVar

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_CB_TABLE_DEFAULT = "cloudflow-circuit-breakers"
F = TypeVar("F", bound=Callable[..., Any])


def _to_int(value: Any) -> int:
    """Coerce a DynamoDB numeric attribute (returned as Decimal) to int."""
    return int(value)


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""
    def __init__(self, name: str, resets_at: float):
        self.resets_at = resets_at
        super().__init__(
            f"Circuit '{name}' is OPEN. Will probe after {int(float(resets_at) - time.time())}s."
        )


class CircuitBreaker:
    """
    DynamoDB-backed circuit breaker.

    Parameters
    ----------
    name:              Unique name for this breaker (e.g., "payment-provider")
    failure_threshold: Number of consecutive failures before opening
    success_threshold: Consecutive successes in HALF_OPEN to close again
    timeout_seconds:   How long to stay OPEN before allowing a probe
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout_seconds: int = 30,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout_seconds = timeout_seconds
        table_name = os.environ.get("CIRCUIT_BREAKER_TABLE", _CB_TABLE_DEFAULT)
        self._table = boto3.resource("dynamodb").Table(table_name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        """
        Execute `fn` through the circuit breaker.
        Raises CircuitBreakerOpenError if the circuit is OPEN.
        """
        state = self._get_state()

        if state["circuit_state"] == CircuitState.OPEN:
            resets_at = float(state.get("resets_at", 0))  # DynamoDB returns Decimal
            if time.time() < resets_at:
                raise CircuitBreakerOpenError(self.name, resets_at)
            # Cooldown elapsed — transition to HALF_OPEN for a probe
            self._transition_to_half_open()
            # Re-read state so _record_success sees HALF_OPEN, not OPEN
            state = self._get_state()

        try:
            result = fn(*args, **kwargs)
            self._record_success(state)
            return result
        except Exception:
            self._record_failure(state)
            raise

    def reset(self) -> None:
        """Manually reset the circuit to CLOSED (for testing / admin ops)."""
        self._table.put_item(Item={
            "name": self.name,
            "circuit_state": CircuitState.CLOSED,
            "failure_count": 0,
            "success_count": 0,
        })

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _get_state(self) -> dict:
        resp = self._table.get_item(Key={"name": self.name})
        return resp.get("Item") or {
            "name": self.name,
            "circuit_state": CircuitState.CLOSED,
            "failure_count": 0,
            "success_count": 0,
        }

    def _record_success(self, prev_state: dict) -> None:
        circuit_state = prev_state.get("circuit_state", CircuitState.CLOSED)

        if circuit_state == CircuitState.HALF_OPEN:
            # Atomic ADD so concurrent probe successes can't lose an increment.
            resp = self._table.update_item(
                Key={"name": self.name},
                UpdateExpression="ADD success_count :one",
                ExpressionAttributeValues={":one": 1},
                ReturnValues="UPDATED_NEW",
            )
            new_successes = _to_int(resp["Attributes"]["success_count"])
            if new_successes >= self.success_threshold:
                # Guard the close on still being HALF_OPEN so a concurrent
                # failure that re-opened the circuit isn't clobbered.
                try:
                    self._table.update_item(
                        Key={"name": self.name},
                        UpdateExpression="SET circuit_state = :s, failure_count = :f, success_count = :sc",
                        ConditionExpression="circuit_state = :half",
                        ExpressionAttributeValues={
                            ":s": CircuitState.CLOSED,
                            ":f": 0,
                            ":sc": 0,
                            ":half": CircuitState.HALF_OPEN,
                        },
                    )
                    logger.info(
                        "Circuit '%s' CLOSED after %d probe successes", self.name, new_successes
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
        # In CLOSED state, a success clears any accumulated failures.
        elif circuit_state == CircuitState.CLOSED:
            self._table.update_item(
                Key={"name": self.name},
                UpdateExpression="SET failure_count = :f",
                ExpressionAttributeValues={":f": 0},
            )

    def _record_failure(self, prev_state: dict) -> None:
        circuit_state = prev_state.get("circuit_state", CircuitState.CLOSED)

        # Atomic ADD so concurrent failures can't lose an increment and leave
        # the breaker below threshold when it should have tripped.
        resp = self._table.update_item(
            Key={"name": self.name},
            UpdateExpression="ADD failure_count :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        new_failures = _to_int(resp["Attributes"]["failure_count"])

        # A failed probe in HALF_OPEN, or crossing the threshold while CLOSED,
        # trips the breaker. Guard on "not already OPEN" so only the first
        # invocation to win the race sets resets_at.
        should_open = (
            circuit_state == CircuitState.HALF_OPEN
            or new_failures >= self.failure_threshold
        )
        if not should_open:
            return

        resets_at = int(time.time() + self.timeout_seconds)
        try:
            self._table.update_item(
                Key={"name": self.name},
                UpdateExpression=(
                    "SET circuit_state = :s, resets_at = :r, success_count = :sc"
                ),
                ConditionExpression="circuit_state <> :open",
                ExpressionAttributeValues={
                    ":s": CircuitState.OPEN,
                    ":r": resets_at,
                    ":sc": 0,
                    ":open": CircuitState.OPEN,
                },
            )
            logger.warning(
                "Circuit '%s' OPENED after %d failures. Resets at %s",
                self.name, new_failures, resets_at,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Already OPEN — another invocation tripped it first. Nothing to do.

    def _transition_to_half_open(self) -> None:
        logger.info("Circuit '%s' transitioning OPEN → HALF_OPEN", self.name)
        self._table.update_item(
            Key={"name": self.name},
            UpdateExpression="SET circuit_state = :s, success_count = :sc",
            ExpressionAttributeValues={":s": CircuitState.HALF_OPEN, ":sc": 0},
        )
