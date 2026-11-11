import os
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import requests


class PayPalError(Exception):
    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


def _paypal_base_url() -> str:
    environment = os.getenv("PAYPAL_ENVIRONMENT", "sandbox").lower()
    if environment == "sandbox":
        return "https://api-m.sandbox.paypal.com"
    raise PayPalError("PAYPAL_ENVIRONMENT must be 'sandbox'; live payments are disabled")


def _access_token(base_url: str, client_id: str, client_secret: str) -> str:
    try:
        response = requests.post(
            f"{base_url}/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise PayPalError("Could not connect to PayPal authorization service") from exc

    if not response.ok:
        raise PayPalError("PayPal authorization failed")
    try:
        token = response.json().get("access_token")
    except (ValueError, AttributeError) as exc:
        raise PayPalError("PayPal returned an invalid authorization response") from exc
    if not isinstance(token, str) or not token:
        raise PayPalError("PayPal authorization response did not contain an access token")
    return token


def create_supplier_payout(
    *,
    request_id: int,
    payout_attempt_id: int,
    sender_batch_id: str,
    recipient_email: str,
    amount: Decimal,
    currency: str = "USD",
) -> dict[str, Any]:
    if currency != "USD":
        raise PayPalError("Supplier payouts currently support USD only")
    result = execute_merchant_payout(
        recipient_email,
        amount,
        item_id=f"{request_id}-{payout_attempt_id}",
        sender_batch_id=sender_batch_id,
    )
    return result


def execute_merchant_payout(
    merchant_email: str,
    amount_usd: Decimal | str | int | float,
    item_id: str | int,
    sender_batch_id: str,
) -> dict[str, Any]:
    if not merchant_email or "@" not in merchant_email:
        raise PayPalError("A valid merchant email is required")
    try:
        normalized_amount = Decimal(str(amount_usd))
    except (InvalidOperation, ValueError) as exc:
        raise PayPalError("Payout amount must be a valid USD amount") from exc
    if not normalized_amount.is_finite() or normalized_amount <= 0:
        raise PayPalError("Payout amount must be greater than zero")
    if normalized_amount.as_tuple().exponent < -2:
        raise PayPalError("Payout amount cannot have more than two decimal places")
    if not str(item_id).strip():
        raise PayPalError("A payout item ID is required")
    if not sender_batch_id or len(sender_batch_id) > 100:
        raise PayPalError("A valid PayPal sender batch ID is required")

    client_id = os.getenv("PAYPAL_CLIENT_ID")
    client_secret = os.getenv("PAYPAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise PayPalError("PayPal credentials are not configured")

    base_url = _paypal_base_url()
    access_token = _access_token(base_url, client_id, client_secret)
    payload = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject": "FormulaFlow supplier payment",
            "email_message": "A FormulaFlow request has been approved for payment.",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {"value": f"{normalized_amount:.2f}", "currency": "USD"},
                "receiver": merchant_email,
                "sender_item_id": str(item_id),
            }
        ],
    }

    try:
        response = requests.post(
            f"{base_url}/v1/payments/payouts",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PayPalError(
            "PayPal payout outcome is unknown; verify the payout in PayPal before retrying",
            outcome_unknown=True,
        ) from exc

    if not response.ok:
        if response.status_code >= 500:
            raise PayPalError(
                "PayPal payout outcome is unknown; verify the payout in PayPal before retrying",
                outcome_unknown=True,
            )
        raise PayPalError("PayPal rejected the supplier payout")

    try:
        result = response.json()
    except ValueError as exc:
        raise PayPalError(
            "PayPal payout outcome is unknown; verify the payout in PayPal before retrying",
            outcome_unknown=True,
        ) from exc

    batch_header = result.get("batch_header") if isinstance(result, dict) else None
    if not isinstance(batch_header, dict):
        raise PayPalError(
            "PayPal payout outcome is unknown; verify the payout in PayPal before retrying",
            outcome_unknown=True,
        )
    batch_id = batch_header.get("payout_batch_id")
    batch_status = batch_header.get("batch_status")
    if not isinstance(batch_id, str) or not batch_id:
        raise PayPalError(
            "PayPal payout outcome is unknown; verify the payout in PayPal before retrying",
            outcome_unknown=True,
        )
    if not isinstance(batch_status, str):
        batch_status = "SUBMITTED"
    return {"payout_batch_id": batch_id, "status": batch_status}


def get_supplier_payout_status(payout_batch_id: str) -> dict[str, str]:
    if not payout_batch_id.strip():
        raise PayPalError("A PayPal payout batch ID is required")
    result = _paypal_api_request(
        "GET",
        f"/v1/payments/payouts/{quote(payout_batch_id, safe='')}",
        request_id=f"formula-payout-status-{uuid4().hex}",
        outcome_unknown_on_error=True,
    )
    batch_header = result.get("batch_header")
    if not isinstance(batch_header, dict):
        raise PayPalError(
            "PayPal payout status response is incomplete",
            outcome_unknown=True,
        )
    batch_status = batch_header.get("batch_status")
    if not isinstance(batch_status, str) or not batch_status:
        raise PayPalError(
            "PayPal payout status response is missing its batch status",
            outcome_unknown=True,
        )

    items = result.get("items")
    item_statuses = [
        item.get("transaction_status")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("transaction_status"), str)
    ] if isinstance(items, list) else []
    if item_statuses:
        final_failures = {"BLOCKED", "CANCELED", "DENIED", "FAILED", "RETURNED"}
        failure = next(
            (item_status for item_status in item_statuses if item_status in final_failures),
            None,
        )
        if failure is not None:
            batch_status = failure
        elif all(item_status == "SUCCESS" for item_status in item_statuses):
            batch_status = "SUCCESS"
        else:
            batch_status = next(
                (item_status for item_status in item_statuses if item_status != "SUCCESS"),
                batch_status,
            )
    return {"status": batch_status}


def _paypal_api_request(
    method: str,
    path: str,
    *,
    request_id: str,
    payload: dict[str, Any] | None = None,
    outcome_unknown_on_error: bool = False,
) -> dict[str, Any]:
    client_id = os.getenv("PAYPAL_CLIENT_ID")
    client_secret = os.getenv("PAYPAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise PayPalError("PayPal credentials are not configured")

    base_url = _paypal_base_url()
    access_token = _access_token(base_url, client_id, client_secret)
    try:
        response = requests.request(
            method,
            f"{base_url}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": request_id,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PayPalError(
            "PayPal payment outcome is unknown; verify the order before retrying",
            outcome_unknown=outcome_unknown_on_error,
        ) from exc

    if not response.ok:
        if outcome_unknown_on_error and response.status_code >= 500:
            raise PayPalError(
                "PayPal payment outcome is unknown; verify the order before retrying",
                outcome_unknown=True,
            )
        raise PayPalError("PayPal could not process the donation order")
    try:
        result = response.json()
    except ValueError as exc:
        raise PayPalError(
            "PayPal returned an invalid response",
            outcome_unknown=outcome_unknown_on_error,
        ) from exc
    if not isinstance(result, dict):
        raise PayPalError(
            "PayPal returned an invalid response",
            outcome_unknown=outcome_unknown_on_error,
        )
    return result


def create_donation_order(
    *, donation_id: int, amount: Decimal, currency: str = "USD"
) -> dict[str, str]:
    result = _paypal_api_request(
        "POST",
        "/v2/checkout/orders",
        request_id=f"formula-donation-order-{donation_id}",
        payload={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": str(donation_id),
                    "description": "FormulaFlow formula support donation",
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
        },
    )
    order_id = result.get("id")
    if not isinstance(order_id, str) or not order_id:
        raise PayPalError("PayPal did not return a valid donation order")
    return {"order_id": order_id}


def capture_donation_order(
    *, donation_id: int, order_id: str
) -> dict[str, str | Decimal]:
    result = _paypal_api_request(
        "POST",
        f"/v2/checkout/orders/{quote(order_id, safe='')}/capture",
        request_id=f"formula-donation-capture-{donation_id}",
        payload={},
        outcome_unknown_on_error=True,
    )
    status_value = result.get("status")
    purchase_units = result.get("purchase_units")
    if not isinstance(purchase_units, list) or not purchase_units:
        raise PayPalError(
            "PayPal capture response is missing purchase details",
            outcome_unknown=True,
        )
    purchase_unit = purchase_units[0]
    if not isinstance(purchase_unit, dict):
        raise PayPalError(
            "PayPal capture response contains invalid purchase details",
            outcome_unknown=True,
        )
    payments = purchase_unit.get("payments")
    captures = payments.get("captures") if isinstance(payments, dict) else None
    if not isinstance(captures, list) or not captures or not isinstance(captures[0], dict):
        raise PayPalError(
            "PayPal capture response is missing capture details",
            outcome_unknown=True,
        )
    capture = captures[0]
    capture_id = capture.get("id")
    capture_status = capture.get("status")
    amount = capture.get("amount")
    if (
        not isinstance(capture_id, str)
        or not capture_id
        or not isinstance(capture_status, str)
        or not isinstance(amount, dict)
        or not isinstance(amount.get("value"), str)
        or not isinstance(amount.get("currency_code"), str)
        or not isinstance(status_value, str)
    ):
        raise PayPalError(
            "PayPal capture response is incomplete",
            outcome_unknown=True,
        )
    try:
        captured_amount = Decimal(amount["value"])
    except (InvalidOperation, ValueError) as exc:
        raise PayPalError(
            "PayPal returned an invalid captured amount",
            outcome_unknown=True,
        ) from exc
    if not captured_amount.is_finite():
        raise PayPalError(
            "PayPal returned an invalid captured amount",
            outcome_unknown=True,
        )
    return {
        "order_status": status_value,
        "capture_id": capture_id,
        "capture_status": capture_status,
        "amount": captured_amount,
        "currency": amount["currency_code"],
    }


def verify_webhook_signature(
    *,
    auth_algo: str,
    cert_url: str,
    transmission_id: str,
    transmission_sig: str,
    transmission_time: str,
    webhook_event: dict[str, Any],
) -> bool:
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        raise PayPalError("PayPal webhook ID is not configured")

    result = _paypal_api_request(
        "POST",
        "/v1/notifications/verify-webhook-signature",
        request_id=f"formula-webhook-verify-{transmission_id}",
        payload={
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
            "webhook_event": webhook_event,
        },
    )
    return result.get("verification_status") == "SUCCESS"