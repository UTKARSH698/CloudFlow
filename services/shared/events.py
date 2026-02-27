"""
CloudFlow Event Schemas
=======================
All inter-service events are defined here as Pydantic models.
Using a shared schema library enforces a contract between producers and consumers —
a form of schema registry without the operational overhead of Confluent Schema Registry.

Design note: events are immutable facts ("OrderCreated") not commands ("CreateOrder").
This distinction matters for event sourcing: commands can be rejected; events cannot.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Domain enums
# ---------------------------------------------------------------------------

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    INVENTORY_RESERVED = "INVENTORY_RESERVED"
    PAYMENT_CHARGED = "PAYMENT_CHARGED"
    CONFIRMED = "CONFIRMED"
    INVENTORY_FAILED = "INVENTORY_FAILED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"


class EventType(str, Enum):
    ORDER_CREATED = "cloudflow.order.created"
    ORDER_CONFIRMED = "cloudflow.order.confirmed"
    ORDER_FAILED = "cloudflow.order.failed"
    INVENTORY_RESERVED = "cloudflow.inventory.reserved"
    INVENTORY_RELEASED = "cloudflow.inventory.released"
    INVENTORY_RESERVE_FAILED = "cloudflow.inventory.reserve_failed"
    PAYMENT_CHARGED = "cloudflow.payment.charged"
    PAYMENT_REFUNDED = "cloudflow.payment.refunded"
    PAYMENT_FAILED = "cloudflow.payment.failed"
    NOTIFICATION_SENT = "cloudflow.notification.sent"


# ---------------------------------------------------------------------------
# Base event envelope
# ---------------------------------------------------------------------------

class CloudFlowEvent(BaseModel):
    """
    Every event is wrapped in this envelope.

    The envelope separates routing metadata (who sent this, when, correlation ID)
    from the domain payload. This is the "outbox pattern" without the outbox —
    because Lambda is the unit of atomicity here.

    correlation_id: traces a single user request across all services.
    causation_id:   the event that caused THIS event (for event graphs).
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    causation_id: str | None = None
    source_service: str
    payload: dict[str, Any]

    def to_eventbridge_entry(self, event_bus_name: str) -> dict:
        """Serialize for EventBridge PutEvents API."""
        return {
            "Source": f"cloudflow.{self.source_service}",
            "DetailType": self.event_type.value,
            "Detail": self.model_dump_json(),
            "EventBusName": event_bus_name,
        }


# ---------------------------------------------------------------------------
# Domain-specific payload models
# ---------------------------------------------------------------------------

class OrderItem(BaseModel):
    product_id: str
    quantity: int = Field(gt=0)
    unit_price_cents: int = Field(ge=0)  # always use integers for money — never floats

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


class CreateOrderRequest(BaseModel):
    customer_id: str
    items: list[OrderItem] = Field(min_length=1)
    idempotency_key: str = Field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def total_cents(self) -> int:
        return sum(item.total_cents for item in self.items)


class OrderCreatedPayload(BaseModel):
    order_id: str
    customer_id: str
    items: list[OrderItem]
    total_cents: int
    idempotency_key: str


class InventoryReservationPayload(BaseModel):
    order_id: str
    reservation_id: str
    items: list[OrderItem]


class PaymentPayload(BaseModel):
    order_id: str
    payment_id: str
    customer_id: str
    amount_cents: int
    status: str


# ---------------------------------------------------------------------------
# Saga state (passed between Step Functions states)
# ---------------------------------------------------------------------------

class SagaContext(BaseModel):
    """
    The data bag passed through the Step Functions state machine.
    Each state can read previous outputs and add its own.
    """
    order_id: str
    customer_id: str
    total_cents: int
    items: list[OrderItem]
    correlation_id: str
    reservation_id: str | None = None
    payment_id: str | None = None
    error: str | None = None
