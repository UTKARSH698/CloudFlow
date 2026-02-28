"""
CloudFlow — Live Demo Dashboard
Visualises the SAGA pattern, circuit breaker, and idempotency in real time.

Usage:
  .\\run.ps1 local-up          # start LocalStack (Docker required)
  .\\run.ps1 dashboard         # open http://localhost:8501
"""
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import streamlit as st
import boto3
from botocore.exceptions import ClientError

# ── Config ─────────────────────────────────────────────────────────────────
ENDPOINT = "http://localhost:4566"
REGION   = "us-east-1"

os.environ.update({
    "AWS_DEFAULT_REGION":    REGION,
    "AWS_ACCESS_KEY_ID":     "test",
    "AWS_SECRET_ACCESS_KEY": "test",
})

T_INVENTORY    = "cloudflow-inventory"
T_ORDERS       = "cloudflow-orders"
T_RESERVATIONS = "cloudflow-reservations"
T_PAYMENTS     = "cloudflow-payments"
T_CB           = "cloudflow-circuit-breaker"

DEMO_PRODUCTS = [
    {"product_id": "LAPTOP-01", "name": "Dev Laptop Pro",  "quantity": Decimal("10"), "price_cents": Decimal("149900")},
    {"product_id": "MOUSE-01",  "name": "Wireless Mouse",  "quantity": Decimal("25"), "price_cents": Decimal("2999")},
    {"product_id": "KEYBD-01",  "name": "Mech Keyboard",   "quantity": Decimal("8"),  "price_cents": Decimal("8999")},
]

# ── AWS clients ────────────────────────────────────────────────────────────
@st.cache_resource
def _db():
    return boto3.resource("dynamodb", endpoint_url=ENDPOINT, region_name=REGION)

@st.cache_resource
def _dbc():
    return boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name=REGION)

def _tbl(name: str):
    return _db().Table(name)

# ── Bootstrap: create tables + seed inventory ─────────────────────────────
def ensure_tables():
    client  = _dbc()
    existing = set(client.list_tables()["TableNames"])

    for name, pk in [
        (T_INVENTORY,    "product_id"),
        (T_ORDERS,       "order_id"),
        (T_RESERVATIONS, "reservation_id"),
        (T_PAYMENTS,     "payment_id"),
        (T_CB,           "service_name"),
    ]:
        if name not in existing:
            client.create_table(
                TableName=name,
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}],
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST",
            )
            client.get_waiter("table_exists").wait(TableName=name)

    inv = _tbl(T_INVENTORY)
    if inv.scan(Limit=1)["Count"] == 0:
        for p in DEMO_PRODUCTS:
            inv.put_item(Item=p)

# ── Data helpers ───────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_inventory() -> list:
    return sorted(_tbl(T_INVENTORY).scan()["Items"], key=lambda x: x["product_id"])

def get_orders() -> list:
    items = _tbl(T_ORDERS).scan()["Items"]
    return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[:15]

def get_cb_state() -> tuple:
    item = _tbl(T_CB).get_item(Key={"service_name": "payment-provider"}).get("Item")
    if not item:
        return "CLOSED", 0, None
    return item.get("state", "CLOSED"), int(item.get("failure_count", 0)), item.get("resets_at")

def trip_circuit_breaker():
    _tbl(T_CB).put_item(Item={
        "service_name": "payment-provider",
        "state":         "OPEN",
        "failure_count": Decimal("3"),
        "resets_at":     Decimal(str(int(time.time()) + 60)),
        "opened_at":     _now(),
    })

def reset_circuit_breaker():
    _tbl(T_CB).put_item(Item={
        "service_name": "payment-provider",
        "state":         "CLOSED",
        "failure_count": Decimal("0"),
    })

# ── SAGA execution ─────────────────────────────────────────────────────────
def run_saga(customer_id: str, product_id: str, quantity: int, force_fail: bool) -> tuple:
    order_id = uuid.uuid4().hex[:8]
    trace    = []

    inv_t  = _tbl(T_INVENTORY)
    res_t  = _tbl(T_RESERVATIONS)
    pay_t  = _tbl(T_PAYMENTS)
    ord_t  = _tbl(T_ORDERS)

    product     = inv_t.get_item(Key={"product_id": product_id}).get("Item", {})
    price_cents = int(product.get("price_cents", 1000)) * quantity

    # ── Step 0: create order (PENDING) ─────────────────────────────────
    ord_t.put_item(Item={
        "order_id":   order_id,
        "customer_id": customer_id,
        "product_id":  product_id,
        "quantity":    Decimal(str(quantity)),
        "status":      "PENDING",
        "created_at":  _now(),
    })
    trace.append({"icon": "🔵", "step": "Create Order",
                  "ms": 0, "detail": f"order_id={order_id}  status=PENDING"})

    # ── Step 1: reserve inventory ───────────────────────────────────────
    t = time.time()
    try:
        inv_t.update_item(
            Key={"product_id": product_id},
            UpdateExpression="SET quantity = quantity - :q",
            ConditionExpression="quantity >= :q",
            ExpressionAttributeValues={":q": Decimal(str(quantity))},
        )
        res_t.put_item(Item={
            "reservation_id": f"res-{order_id}",
            "order_id":        order_id,
            "product_id":      product_id,
            "quantity":        Decimal(str(quantity)),
            "status":          "RESERVED",
            "created_at":      _now(),
        })
        trace.append({"icon": "✅", "step": "Reserve Inventory",
                      "ms": int((time.time() - t) * 1000),
                      "detail": f"reservation_id=res-{order_id}"})
    except ClientError as e:
        detail = "INSUFFICIENT_STOCK — quantity check failed" if "ConditionalCheckFailed" in str(e) else str(e)
        trace.append({"icon": "❌", "step": "Reserve Inventory",
                      "ms": int((time.time() - t) * 1000), "detail": detail})
        ord_t.update_item(Key={"order_id": order_id},
                          UpdateExpression="SET #s=:s",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":s": "FAILED"})
        trace.append({"icon": "🔴", "step": "Order → FAILED", "ms": 0,
                      "detail": "Inventory unchanged — no compensation needed"})
        return order_id, trace

    # ── Step 2: charge payment ──────────────────────────────────────────
    t = time.time()
    cb_state, _, resets_at = get_cb_state()

    payment_error = None
    if cb_state == "OPEN":
        ttl = int(float(resets_at or 0) - time.time())
        payment_error = f"Circuit OPEN — fast-fail (resets in {max(ttl, 0)}s)"
    elif force_fail:
        payment_error = "Payment provider timeout (simulated)"
    else:
        try:
            payment_id = f"pay-{order_id}"
            pay_t.put_item(Item={
                "payment_id":         payment_id,
                "order_id":           order_id,
                "amount_cents":       Decimal(str(price_cents)),
                "status":             "CHARGED",
                "provider_charge_id": f"ch_{uuid.uuid4().hex[:12]}",
                "created_at":         _now(),
            })
            trace.append({"icon": "✅", "step": "Charge Payment",
                          "ms": int((time.time() - t) * 1000),
                          "detail": f"payment_id={payment_id}  amount=${price_cents / 100:.2f}"})
        except Exception as e:
            payment_error = str(e)

    if payment_error:
        trace.append({"icon": "❌", "step": "Charge Payment",
                      "ms": int((time.time() - t) * 1000), "detail": payment_error})

        # ── Compensation ────────────────────────────────────────────────
        t = time.time()
        inv_t.update_item(
            Key={"product_id": product_id},
            UpdateExpression="SET quantity = quantity + :q",
            ExpressionAttributeValues={":q": Decimal(str(quantity))},
        )
        res_t.update_item(
            Key={"reservation_id": f"res-{order_id}"},
            UpdateExpression="SET #s=:s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "RELEASED"},
        )
        trace.append({"icon": "↩️", "step": "↩ Compensate: Release Inventory",
                      "ms": int((time.time() - t) * 1000),
                      "detail": f"Returned {quantity} unit(s) to {product_id} — customer NOT charged"})
        ord_t.update_item(Key={"order_id": order_id},
                          UpdateExpression="SET #s=:s",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":s": "COMPENSATED"})
        trace.append({"icon": "🔴", "step": "Order → COMPENSATED", "ms": 0,
                      "detail": "Inventory restored. No charge applied."})
        return order_id, trace

    # ── Step 3: confirm order ───────────────────────────────────────────
    t = time.time()
    ord_t.update_item(Key={"order_id": order_id},
                      UpdateExpression="SET #s=:s",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "CONFIRMED"})
    trace.append({"icon": "✅", "step": "Confirm Order",
                  "ms": int((time.time() - t) * 1000),
                  "detail": "status=CONFIRMED  — SAGA complete"})
    return order_id, trace


# ── Page layout ────────────────────────────────────────────────────────────
st.set_page_config(page_title="CloudFlow", page_icon="☁️", layout="wide")
st.title("☁️ CloudFlow — Live SAGA Demo")
st.caption("Distributed transaction processing · AWS Step Functions + DynamoDB · LocalStack emulation")

# Connection check
try:
    ensure_tables()
except Exception as e:
    st.error(
        f"**LocalStack not reachable.**\n\n"
        f"Start it first:\n```\n.\\run.ps1 local-up\n```\n\nError: `{e}`"
    )
    st.stop()

st.success("🟢 Connected to LocalStack", icon="✅")

# ── Inventory metrics ──────────────────────────────────────────────────────
st.subheader("📦 Live Inventory")
inventory = get_inventory()
for col, item in zip(st.columns(len(inventory)), inventory):
    qty   = int(item["quantity"])
    color = "🟢" if qty > 5 else ("🟡" if qty > 0 else "🔴")
    col.metric(
        label=item.get("name", item["product_id"]),
        value=f"{qty} units",
        delta=f"${int(item.get('price_cents', 0)) / 100:.2f} each",
        delta_color="off",
    )
    col.caption(f"{color} {item['product_id']}")

st.divider()

# ── Controls | Trace | Circuit Breaker ────────────────────────────────────
left, mid, right = st.columns([1, 1.5, 1])

with left:
    st.subheader("🛒 Place Order")

    product_map  = {item.get("name", item["product_id"]): item["product_id"] for item in inventory}
    customers    = ["alice-001", "bob-002", "charlie-003", "diana-004"]

    customer      = st.selectbox("Customer", customers)
    product_label = st.selectbox("Product", list(product_map.keys()))
    product_id    = product_map[product_label]
    quantity      = st.number_input("Quantity", min_value=1, max_value=10, value=1)

    btn_col1, btn_col2 = st.columns(2)
    place    = btn_col1.button("✅ Place Order",   use_container_width=True, type="primary")
    simulate = btn_col2.button("💥 Force Failure", use_container_width=True)

    st.caption("**Force Failure** simulates a payment timeout → compensation step runs automatically")

with right:
    st.subheader("⚡ Circuit Breaker")
    cb_state, cb_failures, cb_resets_at = get_cb_state()

    badge = {"CLOSED": "🟢 CLOSED", "OPEN": "🔴 OPEN", "HALF_OPEN": "🟡 HALF-OPEN"}.get(cb_state, cb_state)
    st.metric("Payment Provider", badge, f"{cb_failures} recorded failures")

    if cb_state == "OPEN" and cb_resets_at:
        ttl = int(float(cb_resets_at) - time.time())
        st.caption(f"Auto-resets in {max(ttl, 0)}s" if ttl > 0 else "Ready to probe on next request")

    st.divider()
    c1, c2 = st.columns(2)
    if c1.button("Trip CB",  use_container_width=True, help="Force circuit OPEN"):
        trip_circuit_breaker()
        st.rerun()
    if c2.button("Reset CB", use_container_width=True, help="Force circuit CLOSED"):
        reset_circuit_breaker()
        st.rerun()

    st.caption(
        "**Trip** opens the circuit — next payment fast-fails without calling the provider.\n\n"
        "**Reset** closes it for normal flow."
    )

with mid:
    st.subheader("🔄 SAGA Execution Trace")

    if "trace" not in st.session_state:
        st.info("Place an order to see the step-by-step SAGA execution trace with latency per step.")
    else:
        trace    = st.session_state["trace"]
        oid      = st.session_state.get("last_order_id", "")
        total_ms = sum(s["ms"] for s in trace)
        st.caption(f"`order_id={oid}` · total **{total_ms}ms**")

        for step in trace:
            icon   = step["icon"]
            label  = step["step"]
            ms_str = f"`{step['ms']}ms`" if step["ms"] else ""
            detail = step.get("detail", "")

            if icon == "↩️":
                st.markdown(f"↩️ **{label}** {ms_str}")
            else:
                st.markdown(f"{icon} **{label}** {ms_str}")
            if detail:
                st.caption(f"   ↳ {detail}")

# ── Execute on button click ────────────────────────────────────────────────
if place or simulate:
    with st.spinner("Executing SAGA..."):
        oid, trace = run_saga(customer, product_id, int(quantity), force_fail=bool(simulate))
    st.session_state["trace"]         = trace
    st.session_state["last_order_id"] = oid
    st.rerun()

st.divider()

# ── Order history ──────────────────────────────────────────────────────────
st.subheader("📋 Order History")

orders = get_orders()
if not orders:
    st.info("No orders yet — place one above.")
else:
    status_icon = {"CONFIRMED": "✅", "COMPENSATED": "↩️", "FAILED": "❌", "PENDING": "🔵"}
    rows = [
        {
            "Order ID":   o["order_id"],
            "Customer":   o.get("customer_id", ""),
            "Product":    o.get("product_id", ""),
            "Qty":        int(o.get("quantity", 0)),
            "Status":     f"{status_icon.get(o.get('status',''), '⚪')} {o.get('status','')}",
            "Created":    o.get("created_at", "")[:19].replace("T", " "),
        }
        for o in orders
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Controls")

    if st.button("🔄 Refresh Data", use_container_width=True):
        st.rerun()

    auto = st.checkbox("Auto-refresh every 5s")
    if auto:
        time.sleep(5)
        st.rerun()

    st.divider()
    st.markdown("### Demo Scenarios")
    st.markdown("""
1. **Happy path**
   → Place Order

2. **Payment failure + compensation**
   → Force Failure
   *(watch inventory restore itself)*

3. **Circuit breaker fast-fail**
   → Trip CB → Place Order
   *(payment skipped instantly)*

4. **Circuit recovery**
   → Reset CB → Place Order
   *(normal flow resumes)*

5. **Oversell protection**
   → Order qty > stock
   *(ConditionalCheckFailed)*
""")

    st.divider()
    st.caption("CloudFlow · LocalStack · DynamoDB")
    st.caption("github.com/UTKARSH698/CloudFlow")
