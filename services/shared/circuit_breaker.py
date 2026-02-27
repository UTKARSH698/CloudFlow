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
        except Exception as exc:
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
            new_successes = int(prev_state.get("success_count", 0)) + 1
            if new_successes >= self.success_threshold:
                logger.info("Circuit '%s' CLOSED after %d probe successes", self.name, new_successes)
                self._table.update_item(
                    Key={"name": self.name},
                    UpdateExpression="SET circuit_state = :s, failure_count = :f, success_count = :sc",
                    ExpressionAttributeValues={
                        ":s": CircuitState.CLOSED,
                        ":f": 0,
                        ":sc": 0,
                    },
                )
            else:
                self._table.update_item(
                    Key={"name": self.name},
                    UpdateExpression="SET success_count = :sc",
                    ExpressionAttributeValues={":sc": new_successes},
                )
        # In CLOSED state, successes reset the failure counter
        elif circuit_state == CircuitState.CLOSED:
            self._table.update_item(
                Key={"name": self.name},
                UpdateExpression="SET failure_count = :f",
                ExpressionAttributeValues={":f": 0},
            )

    def _record_failure(self, prev_state: dict) -> None:
        new_failures = int(prev_state.get("failure_count", 0)) + 1
        circuit_state = prev_state.get("circuit_state", CircuitState.CLOSED)

        if new_failures >= self.failure_threshold and circuit_state != CircuitState.OPEN:
            resets_at = time.time() + self.timeout_seconds
            logger.warning(
                "Circuit '%s' OPENED after %d failures. Resets at %s",
                self.name, new_failures, resets_at,
            )
            self._table.update_item(
                Key={"name": self.name},
                UpdateExpression=(
                    "SET circuit_state = :s, failure_count = :f, "
                    "resets_at = :r, success_count = :sc"
                ),
                ExpressionAttributeValues={
                    ":s": CircuitState.OPEN,
                    ":f": new_failures,
                    ":r": int(resets_at),
                    ":sc": 0,
                },
            )
        else:
            self._table.update_item(
                Key={"name": self.name},
                UpdateExpression="SET failure_count = :f",
                ExpressionAttributeValues={":f": new_failures},
            )

    def _transition_to_half_open(self) -> None:
        logger.info("Circuit '%s' transitioning OPEN → HALF_OPEN", self.name)
        self._table.update_item(
            Key={"name": self.name},
            UpdateExpression="SET circuit_state = :s, success_count = :sc",
            ExpressionAttributeValues={":s": CircuitState.HALF_OPEN, ":sc": 0},
        )
