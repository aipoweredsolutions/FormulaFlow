import atexit
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from backend import paypal_client
from backend.paypal_client import PayPalError


_test_database = tempfile.TemporaryDirectory()
_test_pickup_token_key = Fernet.generate_key().decode("ascii")
os.environ["DATABASE_URL"] = URL.create(
    "sqlite", database=str(Path(_test_database.name) / "formula-flow-test.db")
).render_as_string(hide_password=False)
from backend import main
from backend.database import (
    Base,
    FormulaRequest,
    DonationRecord,
    PaymentTransaction,
    PayPalWebhookEvent,
    PoolBalance,
    RequestStatus,
    engine,
)

atexit.register(engine.dispose)


def get_valid_admin_auth(client: TestClient) -> dict[str, str]:
    status = client.get("/api/auth/status")
    if status.json().get("registration_open"):
        registration = client.post(
            "/api/auth/register",
            json={
                "email": "operator@example.test",
                "password": "a-secure-test-password",
            },
        )
        if registration.status_code not in {201, 409}:
            raise AssertionError(f"Unexpected registration result: {registration.text}")

    candidate_passwords = [
        ("operator@example.test", "a-secure-test-password"),
        ("operator@example.test", "a-new-secure-test-password"),
        ("manage-admin@example.test", "a-manage-admin-password"),
        ("detail-admin@example.test", "a-detail-admin-password"),
    ]
    for email, password in candidate_passwords:
        response = client.post(
            "/api/auth/login",
            json={"email": email, "password": password},
        )
        if response.status_code == 200:
            return {"Authorization": f"Bearer {response.json()['access_token']}"}
    raise AssertionError("No valid admin credentials were available for the test suite")


class FormulaFlowIntegrationTests(unittest.TestCase):
    def test_existing_donation_records_gain_capture_id(self) -> None:
        from backend import database

        legacy_engine = create_engine("sqlite:///:memory:")
        try:
            with legacy_engine.begin() as connection:
                connection.execute(
                    text("CREATE TABLE formula_requests (id INTEGER PRIMARY KEY)")
                )
                connection.execute(
                    text("INSERT INTO formula_requests (id) VALUES (1)")
                )
                connection.execute(text("CREATE TABLE vendors (id INTEGER PRIMARY KEY)"))
                connection.execute(
                    text(
                        "CREATE TABLE payment_transactions ("
                        "id INTEGER PRIMARY KEY)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TABLE donation_orders ("
                        "id INTEGER PRIMARY KEY, "
                        "paypal_order_id VARCHAR(100), "
                        "paypal_capture_id VARCHAR(100))"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TABLE donation_records ("
                        "id INTEGER PRIMARY KEY, "
                        "paypal_order_id VARCHAR(100), "
                        "amount NUMERIC(10, 2), "
                        "currency VARCHAR(3), "
                        "created_at DATETIME)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO donation_orders "
                        "(id, paypal_order_id, paypal_capture_id) "
                        "VALUES (1, 'ORDER-LEGACY', 'CAPTURE-LEGACY')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO donation_records "
                        "(id, paypal_order_id, amount, currency) "
                        "VALUES (1, 'ORDER-LEGACY', 15.00, 'USD')"
                    )
                )

            with patch.object(database, "engine", legacy_engine):
                database._migrate_existing_columns()

            with legacy_engine.connect() as connection:
                capture_id = connection.execute(
                    text(
                        "SELECT paypal_capture_id FROM donation_records "
                        "WHERE paypal_order_id = 'ORDER-LEGACY'"
                    )
                ).scalar_one()
                self.assertEqual(capture_id, "CAPTURE-LEGACY")
                indexes = connection.execute(
                    text("PRAGMA index_list('donation_records')")
                ).all()
                self.assertTrue(
                    any(
                        row[1] == "uq_donation_records_paypal_capture_id"
                        for row in indexes
                    )
                )
                payout_columns = {
                    row[1]
                    for row in connection.execute(
                        text("PRAGMA table_info('payment_transactions')")
                    ).all()
                }
                self.assertIn("sender_batch_id", payout_columns)
                self.assertIn("payout_mode", payout_columns)
                self.assertIn("containers", payout_columns)
                request_columns = {
                    row[1]
                    for row in connection.execute(
                        text("PRAGMA table_info('formula_requests')")
                    ).all()
                }
                self.assertIn("lifecycle_status", request_columns)
                self.assertIn("pickup_token_expires_at", request_columns)
                self.assertIn("public_reference_code", request_columns)
                self.assertIn("county", request_columns)
                vendor_columns = {
                    row[1]
                    for row in connection.execute(
                        text("PRAGMA table_info('vendors')")
                    ).all()
                }
                self.assertIn("county", vendor_columns)
                reference_code = connection.execute(
                    text(
                        "SELECT public_reference_code FROM formula_requests "
                        "WHERE id = 1"
                    )
                ).scalar_one()
                self.assertRegex(reference_code, r"^[A-Za-z0-9_-]{32}$")
                request_indexes = connection.execute(
                    text("PRAGMA index_list('formula_requests')")
                ).all()
                self.assertTrue(
                    any(
                        row[1] == "uq_formula_requests_public_reference_code"
                        for row in request_indexes
                    )
                )
        finally:
            legacy_engine.dispose()

    def test_paypal_orders_v2_uses_sandbox_and_capture_request(self) -> None:
        token_response = Mock(ok=True)
        token_response.json.return_value = {"access_token": "sandbox-access-token"}
        api_response = Mock(ok=True)
        api_response.json.return_value = {"id": "ORDER-SANDBOX"}

        with (
            patch.dict(
                os.environ,
                {
                    "PAYPAL_ENVIRONMENT": "sandbox",
                    "PAYPAL_CLIENT_ID": "sandbox-client-id",
                    "PAYPAL_CLIENT_SECRET": "sandbox-client-secret",
                },
            ),
            patch.object(
                paypal_client.requests, "post", return_value=token_response
            ) as token_request,
            patch.object(
                paypal_client.requests, "request", return_value=api_response
            ) as api_request,
        ):
            order = paypal_client.create_donation_order(
                donation_id=42,
                amount=Decimal("15.00"),
            )
            self.assertEqual(order["order_id"], "ORDER-SANDBOX")
            token_request.assert_called_once()
            self.assertEqual(
                token_request.call_args.args[0],
                "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            )
            order_call = api_request.call_args
            self.assertEqual(
                order_call.args[1],
                "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            )
            self.assertEqual(
                order_call.kwargs["json"]["purchase_units"][0]["amount"],
                {"currency_code": "USD", "value": "15.00"},
            )

            api_response.json.return_value = {
                "id": "ORDER-SANDBOX",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [
                                {
                                    "id": "CAPTURE-SANDBOX",
                                    "status": "COMPLETED",
                                    "amount": {
                                        "value": "15.00",
                                        "currency_code": "USD",
                                    },
                                }
                            ]
                        }
                    }
                ],
            }
            capture = paypal_client.capture_donation_order(
                donation_id=42,
                order_id="ORDER-SANDBOX",
            )
            self.assertEqual(capture["capture_id"], "CAPTURE-SANDBOX")
            self.assertEqual(
                api_request.call_args.args[1],
                "https://api-m.sandbox.paypal.com/v2/checkout/orders/"
                "ORDER-SANDBOX/capture",
            )

    def test_live_paypal_environment_is_rejected(self) -> None:
        with patch.dict(os.environ, {"PAYPAL_ENVIRONMENT": "live"}):
            with self.assertRaises(PayPalError):
                paypal_client._paypal_base_url()

    def test_paypal_webhook_signature_verification_api_payload(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PAYPAL_ENVIRONMENT": "sandbox",
                    "PAYPAL_CLIENT_ID": "sandbox-client-id",
                    "PAYPAL_CLIENT_SECRET": "sandbox-client-secret",
                    "PAYPAL_WEBHOOK_ID": "WEBHOOK-SANDBOX",
                },
            ),
            patch.object(
                paypal_client,
                "_paypal_api_request",
                return_value={"verification_status": "SUCCESS"},
            ) as api_request,
        ):
            verified = paypal_client.verify_webhook_signature(
                auth_algo="SHA256withRSA",
                cert_url="https://api-m.sandbox.paypal.com/cert",
                transmission_id="TRANSMISSION-1",
                transmission_sig="signature",
                transmission_time="2026-01-01T00:00:00Z",
                webhook_event={"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED"},
            )
        self.assertTrue(verified)
        api_request.assert_called_once()
        self.assertEqual(
            api_request.call_args.args,
            ("POST", "/v1/notifications/verify-webhook-signature"),
        )
        self.assertEqual(
            api_request.call_args.kwargs["payload"]["webhook_id"],
            "WEBHOOK-SANDBOX",
        )
        self.assertEqual(
            api_request.call_args.kwargs["payload"]["transmission_sig"],
            "signature",
        )

    def test_age_bands(self) -> None:
        from backend.business_logic import (
            age_in_months,
            containers_for_age,
        )

        self.assertEqual(containers_for_age(0), 3)
        self.assertEqual(containers_for_age(2), 3)
        self.assertEqual(containers_for_age(3), 4)
        self.assertEqual(containers_for_age(5), 4)
        self.assertEqual(containers_for_age(6), 4)
        self.assertEqual(containers_for_age(120), 4)
        self.assertEqual(
            age_in_months(date(2024, 1, 1), date(2025, 12, 1)),
            23,
        )
        self.assertEqual(
            age_in_months(date(2024, 1, 1), date(2026, 1, 1)),
            24,
        )

    def test_monthly_support_review_and_age_cutoff(self) -> None:
        test_engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(test_engine)
        TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
        db = TestSession()
        try:
            previously_supported = FormulaRequest(
                recipient_name="Monthly beneficiary",
                phone_number="+254700000001",
                location="Kibera",
                county="Nairobi County",
                formula_needs="Formula",
                status="Paid",
                lifecycle_status="collected",
                infant_age_months=22,
                birth_month=date(2024, 1, 1),
                support_month=date(2025, 11, 1),
            )
            db.add(previously_supported)
            db.flush()
            previously_supported.continuity_id = previously_supported.id
            db.add(
                PaymentTransaction(
                    request_id=previously_supported.id,
                    amount=Decimal("40.00"),
                    containers=4,
                    currency="USD",
                    recipient_email="merchant@example.test",
                    status="SUCCESS",
                    payout_mode="PAYPAL",
                )
            )

            cutoff_case = FormulaRequest(
                recipient_name="Cutoff beneficiary",
                phone_number="+254700000002",
                location="Mathare",
                county="Nairobi County",
                formula_needs="Formula",
                status="Pending Verification",
                lifecycle_status="submitted",
                infant_age_months=23,
                birth_month=date(2024, 1, 1),
                support_month=date(2025, 12, 1),
            )
            db.add(cutoff_case)
            db.flush()
            cutoff_case.continuity_id = cutoff_case.id
            db.commit()

            main._ensure_current_monthly_support_reviews(
                db,
                current_month=date(2025, 12, 1),
            )
            renewal = (
                db.query(FormulaRequest)
                .filter(
                    FormulaRequest.continuity_id == previously_supported.id,
                    FormulaRequest.support_month == date(2025, 12, 1),
                )
                .one()
            )
            self.assertEqual(renewal.infant_age_months, 23)
            self.assertEqual(renewal.status, "Pending Verification")
            self.assertIsNone(
                db.query(PaymentTransaction)
                .filter_by(request_id=renewal.id)
                .first()
            )

            main._ensure_current_monthly_support_reviews(
                db,
                current_month=date(2025, 12, 1),
            )
            self.assertEqual(
                db.query(FormulaRequest)
                .filter(
                    FormulaRequest.continuity_id == previously_supported.id,
                    FormulaRequest.support_month == date(2025, 12, 1),
                )
                .count(),
                1,
            )

            main._ensure_current_monthly_support_reviews(
                db,
                current_month=date(2026, 1, 1),
            )
            db.refresh(cutoff_case)
            self.assertEqual(cutoff_case.status, "Rejected")
            self.assertEqual(cutoff_case.lifecycle_status, "rejected")
            self.assertEqual(
                db.query(FormulaRequest)
                .filter(
                    FormulaRequest.continuity_id == previously_supported.id,
                    FormulaRequest.support_month == date(2026, 1, 1),
                )
                .count(),
                0,
            )
            age_overview = main._monthly_support_age_overview(
                db,
                current_month=date(2026, 1, 1),
            )
            ended_cohort = next(
                cohort
                for cohort in age_overview["age_cohorts"]
                if cohort["label"] == "24+ months · ended"
            )
            self.assertEqual(ended_cohort["count"], 1)
        finally:
            db.close()
            test_engine.dispose()

    def test_request_matching_pool_payout_and_pickup_redemption(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PICKUP_TOKEN_ENCRYPTION_KEY": _test_pickup_token_key,
                    "PAYPAL_PAYOUT_SIMULATED": "false",
                },
            ),
            patch.object(
                main,
                "create_donation_order",
                return_value={"order_id": "ORDER-TEST"},
            ) as create_order,
            patch.object(
                main,
                "capture_donation_order",
                return_value={
                    "order_status": "COMPLETED",
                    "capture_id": "CAPTURE-TEST",
                    "capture_status": "COMPLETED",
                    "amount": Decimal("50.00"),
                    "currency": "USD",
                },
            ) as capture_order,
            patch.object(
                main,
                "create_supplier_payout",
                return_value={
                    "payout_batch_id": "BATCH-TEST",
                    "status": "PENDING",
                },
            ) as payout,
            patch.object(
                main,
                "get_supplier_payout_status",
                return_value={"status": "SUCCESS"},
            ) as payout_status_check,
            TestClient(main.app) as client,
        ):
            auth = get_valid_admin_auth(client)
            health = client.get("/health")
            self.assertEqual(health.status_code, 200, health.text)
            self.assertEqual(health.json(), {"status": "ok"})
            registration = client.post(
                "/api/auth/register",
                json={
                    "email": "operator@example.test",
                    "password": "a-secure-test-password",
                },
            )
            self.assertIn(
                registration.status_code,
                {201, 409},
                registration.text,
            )
            if registration.status_code == 201:
                self.assertEqual(registration.json()["role"], "admin")
                self.assertNotIn("password_hash", registration.json())
            blocked_registration = client.post(
                "/api/auth/register",
                json={
                    "email": "second@example.test",
                    "password": "another-secure-password",
                },
            )
            self.assertEqual(blocked_registration.status_code, 409)
            bad_login = client.post(
                "/api/auth/login",
                json={
                    "email": "operator@example.test",
                    "password": "wrong-password",
                },
            )
            self.assertEqual(bad_login.status_code, 401)
            login = None
            for candidate_password in (
                "a-secure-test-password",
                "a-new-secure-test-password",
            ):
                candidate = client.post(
                    "/api/auth/login",
                    json={
                        "email": "OPERATOR@example.test",
                        "password": candidate_password,
                    },
                )
                if candidate.status_code == 200:
                    login = candidate
                    break
            self.assertIsNotNone(login)
            self.assertEqual(login.status_code, 200, login.text)
            auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
            unauthenticated_llm = client.post(
                "/api/llm",
                json={"input": []},
            )
            self.assertEqual(unauthenticated_llm.status_code, 401)
            upstream = Mock(ok=True, status_code=200)
            upstream.iter_content.return_value = iter(
                [
                    b'data: {"type":"response.output_item.added","item":{"id":"fc-item","type":"function_call","call_id":"call-1","name":"view_schema"}}\n\n'
                    b'data: {"type":"response.function_call_arguments.delta","item_id":"fc-item","delta":"{}"}\n\n'
                    b'data: {"type":"response.output_item.done","item":{"id":"fc-item","type":"function_call","call_id":"call-1","name":"view_schema","arguments":"{}"}}\n\n'
                    b'data: {"type":"response.completed","response":{"id":"resp-1","created_at":1762300000,"model":"model-test","status":"completed","output":[{"id":"fc-item","type":"function_call","call_id":"call-1","name":"view_schema","arguments":"{}","status":"completed"}]}}\n\n'
                ]
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": "test-key",
                        "OPENAI_MODEL": "model-test",
                    },
                ),
                patch.object(main.requests, "post", return_value=upstream) as llm_call,
            ):
                llm_response = client.post(
                    "/api/llm",
                    headers=auth,
                    json={
                        "input": [
                            {
                                "kind": "input",
                                "type": "message",
                                "role": "user",
                                "status": "completed",
                                "content": [{"type": "text", "text": "Inspect data"}],
                            }
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "name": "view_schema",
                                "description": "Inspect dashboard schema",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                },
                            }
                        ],
                    },
                )
            self.assertEqual(llm_response.status_code, 200, llm_response.text)
            self.assertIn("response.function_call_arguments.delta", llm_response.text)
            self.assertEqual(llm_call.call_args.args[0], "https://api.openai.com/v1/responses")
            self.assertEqual(
                llm_call.call_args.kwargs["json"]["model"],
                "model-test",
            )
            self.assertTrue(llm_call.call_args.kwargs["json"]["stream"])
            self.assertFalse(llm_call.call_args.kwargs["json"]["store"])
            self.assertEqual(
                llm_call.call_args.kwargs["json"]["tools"][0]["name"],
                "view_schema",
            )
            account_id = login.json()["id"]
            main._llm_request_times.pop(account_id, None)
            rate_limit_upstream = Mock(ok=True, status_code=200)
            rate_limit_upstream.iter_content.return_value = iter([])
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": "test-key",
                        "OPENAI_MODEL": "model-test",
                    },
                ),
                patch.object(main, "LLM_RATE_LIMIT", 1),
                patch.object(
                    main.requests, "post", return_value=rate_limit_upstream
                ) as rate_limited_call,
            ):
                first_ai_request = client.post(
                    "/api/llm",
                    headers=auth,
                    json={"input": [{"type": "message", "role": "user"}]},
                )
                limited_ai_request = client.post(
                    "/api/llm",
                    headers=auth,
                    json={"input": [{"type": "message", "role": "user"}]},
                )
            self.assertEqual(first_ai_request.status_code, 200)
            self.assertEqual(limited_ai_request.status_code, 429)
            self.assertIn("Retry-After", limited_ai_request.headers)
            self.assertEqual(rate_limited_call.call_count, 1)
            main._llm_request_times.pop(account_id, None)
            current_admin = client.get("/api/auth/me", headers=auth)
            self.assertEqual(current_admin.status_code, 200)
            self.assertEqual(current_admin.json()["email"], "operator@example.test")
            unauthenticated = client.get("/api/admin/dashboard")
            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(
                client.post(
                    "/api/auth/change-password",
                    json={
                        "current_password": "a-secure-test-password",
                        "new_password": "a-new-secure-test-password",
                    },
                ).status_code,
                401,
            )
            for path in (
                "/api/admin/studio/donations",
                "/api/admin/studio/payouts",
                "/api/admin/studio/requests",
                "/api/admin/studio/merchants",
                "/api/admin/studio/merchant-price-history",
            ):
                self.assertEqual(client.get(path).status_code, 401)
            preflight = client.options(
                "/api/admin/requests/1/decision",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            self.assertEqual(preflight.status_code, 200, preflight.text)
            self.assertIn(
                "authorization",
                preflight.headers["access-control-allow-headers"].lower(),
            )
            merchant = client.put(
                "/api/admin/vendors/Kibera%20Pharmacy",
                headers=auth,
                json={
                    "name": "Kibera Pharmacy",
                    "location": "Kibera, Nairobi",
                    "paypal_email": "merchant@example.test",
                    "price_per_container_usd": "10.00",
                },
            )
            self.assertEqual(merchant.status_code, 200, merchant.text)

            order = client.post("/api/donations/orders", json={"amount": "50.00"})
            self.assertEqual(order.status_code, 201, order.text)
            capture = client.post(
                "/api/donations/orders/ORDER-TEST/capture"
            )
            self.assertEqual(capture.status_code, 200, capture.text)
            self.assertEqual(capture.json()["status"], "COMPLETED")
            self.assertEqual(capture.json()["capture_id"], "CAPTURE-TEST")
            self.assertIsNotNone(capture.json()["donation_id"])

            repeated_capture = client.post(
                "/api/donations/orders/ORDER-TEST/capture"
            )
            self.assertEqual(repeated_capture.status_code, 200, repeated_capture.text)
            self.assertEqual(repeated_capture.json()["donation_id"], capture.json()["donation_id"])

            recorded = client.post(
                "/api/donations/record",
                json={"paypal_order_id": "ORDER-TEST", "amount": "50.00"},
            )
            self.assertEqual(recorded.status_code, 201, recorded.text)
            self.assertEqual(recorded.json()["paypal_capture_id"], "CAPTURE-TEST")
            self.assertEqual(recorded.json()["id"], capture.json()["donation_id"])
            duplicate_record = client.post(
                "/api/donations/record",
                json={"paypal_order_id": "ORDER-TEST", "amount": "50.00"},
            )
            self.assertEqual(duplicate_record.json()["id"], recorded.json()["id"])

            create_order.return_value = {"order_id": "ORDER-FAILED"}
            failed_order = client.post(
                "/api/donations/orders", json={"amount": "20.00"}
            )
            self.assertEqual(failed_order.status_code, 201, failed_order.text)
            capture_order.side_effect = PayPalError(
                "PayPal payment outcome is unknown; verify the order before retrying",
                outcome_unknown=True,
            )
            failed_capture = client.post(
                "/api/donations/orders/ORDER-FAILED/capture"
            )
            self.assertEqual(failed_capture.status_code, 502, failed_capture.text)
            self.assertIn("verify the order", failed_capture.json()["detail"])
            self.assertEqual(
                client.get("/api/public/impact").json()["total_donations_usd"],
                "50.00",
            )
            capture_order.side_effect = None

            impact = client.get("/api/public/impact")
            self.assertEqual(impact.status_code, 200, impact.text)
            self.assertEqual(impact.json()["total_donations_usd"], "50.00")
            self.assertEqual(impact.json()["verified_merchants"], 1)

            newborn = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Amina",
                    "phone_number": "+254700000000",
                    "location": "Kibera",
                    "county": "Nairobi County",
                    "infant_age_months": 2,
                },
            )
            self.assertEqual(newborn.status_code, 201, newborn.text)
            reference_code = newborn.json()["public_reference_code"]
            self.assertRegex(reference_code, r"^[A-Za-z0-9_-]{32}$")
            self.assertEqual(newborn.json()["status"], "Pending Verification")
            self.assertEqual(newborn.json()["county"], "Nairobi County")
            self.assertIsNone(newborn.json()["matched_vendor"])
            self.assertIsNone(newborn.json()["containers_per_month"])
            self.assertIsNone(newborn.json()["total_cost_usd"])
            submitted_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": reference_code},
            )
            self.assertEqual(
                submitted_public_status.status_code,
                200,
                submitted_public_status.text,
            )
            self.assertEqual(
                submitted_public_status.json()["stage"],
                "submitted",
            )
            self.assertEqual(
                set(submitted_public_status.json()),
                {"stage", "last_updated_at"},
            )
            self.assertNotIn("recipient_name", submitted_public_status.json())
            self.assertEqual(
                submitted_public_status.headers["Cache-Control"],
                "no-store",
            )
            unknown_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": "A" * 32},
            )
            self.assertEqual(unknown_public_status.status_code, 404)
            malformed_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": "guessable"},
            )
            self.assertEqual(malformed_public_status.status_code, 422)
            early_match = client.post(
                f"/api/admin/requests/{newborn.json()['id']}/match",
                headers=auth,
                json={"merchant_id": merchant.json()["id"]},
            )
            self.assertEqual(early_match.status_code, 409)
            approved = client.post(
                f"/api/admin/requests/{newborn.json()['id']}/decision",
                headers=auth,
                json={
                    "decision": "approve",
                    "note": "Eligibility verified.",
                },
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["status"], "Approved")
            approved_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": reference_code},
            )
            self.assertEqual(approved_public_status.status_code, 200)
            self.assertEqual(approved_public_status.json()["stage"], "approved")
            duplicate_decision = client.post(
                f"/api/admin/requests/{newborn.json()['id']}/decision",
                headers=auth,
                json={"decision": "reject", "note": "Already reviewed."},
            )
            self.assertEqual(duplicate_decision.status_code, 409)
            matched = client.post(
                f"/api/admin/requests/{newborn.json()['id']}/match",
                headers=auth,
                json={
                    "merchant_id": merchant.json()["id"],
                    "note": "Closest available retailer confirmed.",
                },
            )
            self.assertEqual(matched.status_code, 200, matched.text)
            self.assertEqual(matched.json()["containers_per_month"], 3)
            self.assertEqual(matched.json()["total_cost_usd"], "30.00")

            rejected_case = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Zawadi",
                    "phone_number": "+254700000002",
                    "location": "Kibera",
                    "county": "Nairobi County",
                    "infant_age_months": 1,
                },
            )
            self.assertEqual(rejected_case.status_code, 201, rejected_case.text)
            self.assertNotEqual(
                rejected_case.json()["public_reference_code"],
                reference_code,
            )
            rejected_without_note = client.post(
                f"/api/admin/requests/{rejected_case.json()['id']}/decision",
                headers=auth,
                json={"decision": "reject"},
            )
            self.assertEqual(rejected_without_note.status_code, 422)
            rejected = client.post(
                f"/api/admin/requests/{rejected_case.json()['id']}/decision",
                headers=auth,
                json={
                    "decision": "reject",
                    "note": "The submitted details could not be verified.",
                },
            )
            self.assertEqual(rejected.status_code, 200, rejected.text)
            self.assertEqual(rejected.json()["status"], "Rejected")
            rejected_public_status = client.post(
                "/api/public/request-status",
                json={
                    "reference_code": rejected_case.json()["public_reference_code"]
                },
            )
            self.assertEqual(rejected_public_status.status_code, 404)
            rejected_match = client.post(
                f"/api/admin/requests/{rejected_case.json()['id']}/match",
                headers=auth,
                json={"merchant_id": merchant.json()["id"]},
            )
            self.assertEqual(rejected_match.status_code, 409)
            history = client.get(
                f"/api/admin/requests/{rejected_case.json()['id']}/history",
                headers=auth,
            )
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(
                [event["action"] for event in history.json()],
                ["REQUEST_SUBMITTED", "REQUEST_REJECTED"],
            )
            self.assertEqual(history.json()[-1]["actor"], "operator@example.test")
            self.assertIn("could not be verified", history.json()[-1]["note"])
            over_age_limit = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Amina",
                    "phone_number": "+254700000000",
                    "location": "Kibera",
                    "county": "Nairobi County",
                    "infant_age_months": 25,
                },
            )
            self.assertEqual(over_age_limit.status_code, 422)

            missing_county = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Amina",
                    "phone_number": "+254700000000",
                    "location": "Kibera",
                    "infant_age_months": 2,
                },
            )
            self.assertEqual(missing_county.status_code, 422)

            older_infant = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Wanjiku",
                    "phone_number": "+254700000001",
                    "location": "Kibera, Nairobi",
                    "county": "Nairobi County",
                    "infant_age_months": 3,
                },
            )
            self.assertEqual(older_infant.status_code, 201, older_infant.text)
            self.assertIsNone(older_infant.json()["matched_vendor"])
            older_approval = client.post(
                f"/api/admin/requests/{older_infant.json()['id']}/decision",
                headers=auth,
                json={"decision": "approve"},
            )
            self.assertEqual(older_approval.status_code, 200, older_approval.text)
            older_match = client.post(
                f"/api/admin/requests/{older_infant.json()['id']}/match",
                headers=auth,
                json={"merchant_id": merchant.json()["id"]},
            )
            self.assertEqual(older_match.status_code, 200, older_match.text)
            self.assertEqual(older_match.json()["containers_per_month"], 4)
            self.assertEqual(older_match.json()["total_cost_usd"], "40.00")

            payment = client.post(
                f"/api/admin/approve/{newborn.json()['id']}",
                headers=auth,
            )
            self.assertEqual(payment.status_code, 200, payment.text)
            payout_session = main.SessionLocal()
            try:
                submitted_transaction = (
                    payout_session.query(PaymentTransaction)
                    .filter_by(request_id=newborn.json()["id"])
                    .one()
                )
                self.assertTrue(submitted_transaction.sender_batch_id)
                self.assertEqual(
                    payout.call_args.kwargs["sender_batch_id"],
                    submitted_transaction.sender_batch_id,
                )
            finally:
                payout_session.close()
            payment_body = payment.json()
            self.assertEqual(payment_body["status"], "Approved")
            self.assertEqual(payment_body["lifecycle_status"], "payout_pending")
            self.assertEqual(payment_body["payout_status"], "PENDING")
            self.assertIsNone(payment_body["pickup_token"])
            pending_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": reference_code},
            )
            self.assertEqual(pending_public_status.status_code, 200)
            self.assertEqual(pending_public_status.json()["stage"], "approved")
            finalized_payment = client.post(
                f"/api/admin/payout/{newborn.json()['id']}/refresh",
                headers=auth,
            )
            self.assertEqual(finalized_payment.status_code, 200, finalized_payment.text)
            finalized_body = finalized_payment.json()
            self.assertEqual(finalized_body["status"], "Paid")
            self.assertEqual(
                finalized_body["lifecycle_status"],
                "ready_for_pickup",
            )
            self.assertIsNone(finalized_body["pickup_token"])
            self.assertEqual(finalized_body["payout_status"], "SUCCESS")
            payout_status_check.assert_called_once_with("BATCH-TEST")
            refreshed_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": reference_code},
            )
            self.assertEqual(refreshed_public_status.status_code, 200)
            self.assertEqual(
                refreshed_public_status.json()["stage"],
                "ready_for_pickup",
            )

            webhook_headers = {
                "PayPal-Auth-Algo": "SHA256withRSA",
                "PayPal-Cert-Url": "https://api-m.sandbox.paypal.com/cert",
                "PayPal-Transmission-Id": "TRANSMISSION-1",
                "PayPal-Transmission-Sig": "signature",
                "PayPal-Transmission-Time": "2026-01-01T00:00:00Z",
            }
            payout_success_event = {
                "id": "WH-PAYOUT-SUCCESS",
                "event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
                "resource": {
                    "batch_header": {
                        "payout_batch_id": "BATCH-TEST",
                        "batch_status": "SUCCESS",
                        "sender_batch_header": {
                            "sender_batch_id": submitted_transaction.sender_batch_id,
                        },
                    }
                },
            }
            with patch.object(main, "verify_webhook_signature", return_value=True):
                payout_success_webhook = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json=payout_success_event,
                )
                self.assertEqual(
                    payout_success_webhook.status_code,
                    200,
                    payout_success_webhook.text,
                )
                self.assertFalse(payout_success_webhook.json()["duplicate"])
            ready_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": reference_code},
            )
            self.assertEqual(ready_public_status.status_code, 200)
            self.assertEqual(
                ready_public_status.json()["stage"],
                "ready_for_pickup",
            )
            ready_request = next(
                item
                for item in client.get("/api/admin/requests", headers=auth).json()
                if item["id"] == newborn.json()["id"]
            )
            self.assertEqual(ready_request["status"], "Paid")
            self.assertEqual(ready_request["lifecycle_status"], "ready_for_pickup")
            self.assertIsNotNone(ready_request["payout_confirmed_at"])
            self.assertIsNotNone(ready_request["ready_for_pickup_at"])
            self.assertIsNotNone(ready_request["pickup_token_expires_at"])
            token_session = main.SessionLocal()
            try:
                token_request = token_session.get(
                    FormulaRequest, newborn.json()["id"]
                )
                self.assertIsNotNone(token_request.pickup_token_hash)
                self.assertIsNotNone(token_request.pickup_token_encrypted)
                self.assertIsNone(token_request.pickup_token_retrieved_at)
            finally:
                token_session.close()
            token_response = client.post(
                f"/api/admin/requests/{newborn.json()['id']}/pickup-token/retrieve",
                headers=auth,
            )
            self.assertEqual(token_response.status_code, 200, token_response.text)
            pickup_token = token_response.json()["token"]
            self.assertTrue(pickup_token)
            self.assertEqual(
                client.post(
                    f"/api/admin/requests/{newborn.json()['id']}/pickup-token/retrieve",
                    headers=auth,
                ).status_code,
                409,
            )
            impact = client.get("/api/public/impact")
            self.assertEqual(impact.json()["infants_sustained"], 1)
            payout.assert_called_once()

            insufficient = client.post(
                f"/api/admin/approve/{older_infant.json()['id']}",
                headers=auth,
            )
            self.assertEqual(insufficient.status_code, 409)
            self.assertIn("Insufficient donation pool funds", insufficient.json()["detail"])

            dashboard = client.get("/api/admin/dashboard", headers=auth)
            self.assertEqual(dashboard.status_code, 200, dashboard.text)
            self.assertEqual(dashboard.json()["available_pool_usd"], "20.00")
            self.assertEqual(
                client.get("/api/public/impact").json()["total_donations_usd"],
                "50.00",
            )

            studio_donations = client.get(
                "/api/admin/studio/donations", headers=auth
            )
            self.assertEqual(studio_donations.status_code, 200, studio_donations.text)
            self.assertEqual(len(studio_donations.json()), 1)
            self.assertNotIn("paypal_order_id", studio_donations.json()[0])
            self.assertIsInstance(studio_donations.json()[0]["amount"], float)

            studio_payouts = client.get("/api/admin/studio/payouts", headers=auth)
            self.assertEqual(studio_payouts.status_code, 200, studio_payouts.text)
            self.assertEqual(len(studio_payouts.json()), 1)
            self.assertNotIn("recipient_email", studio_payouts.json()[0])
            self.assertIsInstance(studio_payouts.json()[0]["amount"], float)

            studio_requests = client.get("/api/admin/studio/requests", headers=auth)
            self.assertEqual(studio_requests.status_code, 200, studio_requests.text)
            self.assertNotIn("recipient_name", studio_requests.json()[0])
            self.assertNotIn("phone_number", studio_requests.json()[0])

            studio_merchants = client.get("/api/admin/studio/merchants", headers=auth)
            self.assertEqual(studio_merchants.status_code, 200, studio_merchants.text)
            self.assertEqual(len(studio_merchants.json()), 1)
            self.assertNotIn("paypal_email", studio_merchants.json()[0])

            redeemed = client.post(
                "/api/pickup/redeem",
                json={"token": pickup_token},
            )
            self.assertEqual(redeemed.status_code, 200, redeemed.text)
            self.assertEqual(redeemed.json()["status"], "redeemed")
            collected_public_status = client.post(
                "/api/public/request-status",
                json={"reference_code": reference_code},
            )
            self.assertEqual(collected_public_status.status_code, 200)
            self.assertEqual(
                collected_public_status.json()["stage"],
                "collected",
            )
            repeated_redemption = client.post(
                "/api/pickup/redeem",
                json={"token": pickup_token},
            )
            self.assertEqual(repeated_redemption.status_code, 409)
            main._status_lookup_times.clear()
            with patch.object(main, "STATUS_LOOKUP_RATE_LIMIT", 1):
                allowed_status_lookup = client.post(
                    "/api/public/request-status",
                    json={"reference_code": reference_code},
                )
                limited_status_lookup = client.post(
                    "/api/public/request-status",
                    json={"reference_code": reference_code},
                )
            self.assertEqual(allowed_status_lookup.status_code, 200)
            self.assertEqual(limited_status_lookup.status_code, 429)
            self.assertIn("Retry-After", limited_status_lookup.headers)
            expiry_session = main.SessionLocal()
            try:
                expired_request = FormulaRequest(
                    recipient_name="Synthetic Expiry Recipient",
                    phone_number="demo-only",
                    location="Demo Location",
                    formula_needs="Demo request",
                    status=RequestStatus.PAID.value,
                    lifecycle_status="ready_for_pickup",
                    pickup_token_hash=sha256(
                        b"expired-demo-token-is-long"
                    ).hexdigest(),
                    pickup_token_expires_at=datetime.now(timezone.utc)
                    - timedelta(minutes=1),
                )
                expiry_session.add(expired_request)
                expiry_session.commit()
            finally:
                expiry_session.close()
            expired_redemption = client.post(
                "/api/pickup/redeem",
                json={"token": "expired-demo-token-is-long"},
            )
            self.assertEqual(expired_redemption.status_code, 410)
            completed_history = client.get(
                f"/api/admin/requests/{newborn.json()['id']}/history",
                headers=auth,
            )
            self.assertEqual(completed_history.status_code, 200)
            self.assertEqual(
                [event["action"] for event in completed_history.json()],
                [
                    "REQUEST_SUBMITTED",
                    "REQUEST_APPROVED",
                    "MERCHANT_MATCHED",
                    "PAYOUT_SUBMITTED",
                    "PAYOUT_STATUS_UPDATED",
                    "PAYOUT_CONFIRMED",
                    "PICKUP_READY",
                    "PICKUP_TOKEN_RETRIEVED",
                    "PICKUP_REDEEMED",
                ],
            )

            price_update = client.put(
                "/api/admin/vendors/Kibera%20Pharmacy",
                headers=auth,
                json={
                    "name": "Kibera Pharmacy",
                    "location": "Kibera, Nairobi",
                    "paypal_email": "merchant@example.test",
                    "price_per_container_usd": "15.00",
                },
            )
            self.assertEqual(price_update.status_code, 200, price_update.text)
            price_history = client.get(
                "/api/admin/studio/merchant-price-history",
                headers=auth,
            )
            self.assertEqual(price_history.status_code, 200, price_history.text)
            self.assertEqual(len(price_history.json()), 1)
            self.assertEqual(
                price_history.json()[0]["previous_price_per_container_usd"],
                10.0,
            )
            self.assertEqual(
                price_history.json()[0]["price_per_container_usd"],
                15.0,
            )
            self.assertEqual(price_history.json()[0]["change_percent"], 50.0)
            anomalies = client.get("/api/admin/anomalies", headers=auth)
            self.assertEqual(anomalies.status_code, 200, anomalies.text)
            self.assertEqual(len(anomalies.json()["price_alerts"]), 1)

            answer = client.post(
                "/api/admin/agent/query",
                headers=auth,
                json={"question": "How many pending requests are in Kibera?"},
            )
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertIn("0 requests awaiting verification", answer.json()["answer"])

            create_order.return_value = {"order_id": "ORDER-WEBHOOK"}
            webhook_order = client.post(
                "/api/donations/orders", json={"amount": "5.00"}
            )
            self.assertEqual(webhook_order.status_code, 201, webhook_order.text)
            capture_order.side_effect = PayPalError(
                "PayPal payment outcome is unknown; verify the order before retrying",
                outcome_unknown=True,
            )
            uncertain_capture = client.post(
                "/api/donations/orders/ORDER-WEBHOOK/capture"
            )
            self.assertEqual(uncertain_capture.status_code, 502)
            capture_order.side_effect = None
            capture_event = {
                "id": "WH-CAPTURE-1",
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {
                    "id": "CAPTURE-WEBHOOK",
                    "status": "COMPLETED",
                    "amount": {"value": "5.00", "currency_code": "USD"},
                    "supplementary_data": {
                        "related_ids": {"order_id": "ORDER-WEBHOOK"}
                    },
                },
            }
            webhook_headers = {
                "PayPal-Auth-Algo": "SHA256withRSA",
                "PayPal-Cert-Url": "https://api-m.sandbox.paypal.com/cert",
                "PayPal-Transmission-Id": "TRANSMISSION-1",
                "PayPal-Transmission-Sig": "signature",
                "PayPal-Transmission-Time": "2026-01-01T00:00:00Z",
            }
            with patch.object(main, "verify_webhook_signature", return_value=True):
                capture_webhook = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json=capture_event,
                )
                self.assertEqual(capture_webhook.status_code, 200, capture_webhook.text)
                self.assertFalse(capture_webhook.json()["duplicate"])
                duplicate_webhook = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json=capture_event,
                )
                self.assertEqual(duplicate_webhook.status_code, 200)
                self.assertTrue(duplicate_webhook.json()["duplicate"])

            webhook_impact = client.get("/api/public/impact").json()
            self.assertEqual(webhook_impact["total_donations_usd"], "55.00")
            webhook_dashboard = client.get(
                "/api/admin/dashboard", headers=auth
            ).json()
            self.assertEqual(webhook_dashboard["available_pool_usd"], "25.00")
            webhook_session = main.SessionLocal()
            try:
                webhook_record = webhook_session.query(DonationRecord).filter_by(
                    paypal_order_id="ORDER-WEBHOOK"
                ).one()
                self.assertEqual(
                    webhook_record.paypal_capture_id, "CAPTURE-WEBHOOK"
                )
                self.assertEqual(
                    webhook_session.query(PayPalWebhookEvent)
                    .filter_by(event_id="WH-CAPTURE-1")
                    .count(),
                    1,
                )
            finally:
                webhook_session.close()

            with patch.object(main, "verify_webhook_signature", return_value=False):
                with self.assertLogs("backend.main", level="WARNING") as logs:
                    unverified = client.post(
                        "/api/paypal/webhook",
                        headers=webhook_headers,
                        json={
                            **capture_event,
                            "id": "WH-UNVERIFIED",
                        },
                    )
            self.assertEqual(unverified.status_code, 400)
            self.assertTrue(
                any("Rejected unverified PayPal webhook" in line for line in logs.output)
            )
            with patch.object(
                main,
                "verify_webhook_signature",
                side_effect=PayPalError("verification service unavailable"),
            ):
                verification_unavailable = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json={**capture_event, "id": "WH-VERIFY-UNAVAILABLE"},
                )
            self.assertEqual(verification_unavailable.status_code, 503)
            webhook_session = main.SessionLocal()
            try:
                self.assertEqual(
                    webhook_session.query(PayPalWebhookEvent)
                    .filter_by(event_id="WH-UNVERIFIED")
                    .count(),
                    0,
                )
                self.assertEqual(
                    webhook_session.query(PayPalWebhookEvent)
                    .filter_by(event_id="WH-VERIFY-UNAVAILABLE")
                    .count(),
                    0,
                )
            finally:
                webhook_session.close()
            self.assertEqual(
                client.get("/api/public/impact").json()["total_donations_usd"],
                "55.00",
            )

            payout_session = main.SessionLocal()
            try:
                webhook_request = FormulaRequest(
                    recipient_name="Synthetic Demo Recipient",
                    phone_number="demo-only",
                    location="Demo Location",
                    formula_needs="Demo request",
                    status=RequestStatus.APPROVED.value,
                )
                payout_session.add(webhook_request)
                payout_session.flush()
                webhook_payout = PaymentTransaction(
                    request_id=webhook_request.id,
                    amount=Decimal("5.00"),
                    currency="USD",
                    recipient_email="synthetic@example.test",
                    sender_batch_id="SENDER-WEBHOOK",
                    status="PROCESSING",
                )
                payout_session.add(webhook_payout)
                pool = payout_session.get(PoolBalance, 1)
                pool.available_usd -= Decimal("5.00")
                payout_session.commit()
                webhook_payout_id = webhook_payout.id
                webhook_request_id = webhook_request.id
            finally:
                payout_session.close()

            payout_event = {
                "id": "WH-PAYOUT-1",
                "event_type": "PAYMENT.PAYOUTSBATCH.DENIED",
                "resource": {
                    "batch_header": {
                        "payout_batch_id": "BATCH-WEBHOOK",
                        "sender_batch_header": {
                            "sender_batch_id": "SENDER-WEBHOOK",
                        },
                        "batch_status": "DENIED",
                    }
                },
            }
            with patch.object(main, "verify_webhook_signature", return_value=True):
                unmatched_payout_webhook = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json={
                        **payout_event,
                        "resource": {
                            "batch_header": {
                                "payout_batch_id": "BATCH-NOT-YET-MATCHED",
                            }
                        },
                    },
                )
                self.assertEqual(
                    unmatched_payout_webhook.status_code,
                    503,
                    unmatched_payout_webhook.text,
                )
                payout_session = main.SessionLocal()
                try:
                    self.assertIsNone(
                        payout_session.query(PayPalWebhookEvent)
                        .filter_by(event_id="WH-PAYOUT-1")
                        .one_or_none()
                    )
                finally:
                    payout_session.close()

                payout_webhook = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json=payout_event,
                )
                self.assertEqual(payout_webhook.status_code, 200, payout_webhook.text)
                duplicate_payout_webhook = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json=payout_event,
                )
                self.assertTrue(duplicate_payout_webhook.json()["duplicate"])
                repeated_failure_event = client.post(
                    "/api/paypal/webhook",
                    headers=webhook_headers,
                    json={**payout_event, "id": "WH-PAYOUT-2"},
                )
                self.assertEqual(repeated_failure_event.status_code, 200)
                self.assertFalse(repeated_failure_event.json()["duplicate"])
            payout_session = main.SessionLocal()
            try:
                self.assertEqual(
                    payout_session.get(PaymentTransaction, webhook_payout_id).status,
                    "FAILED",
                )
                self.assertEqual(
                    payout_session.get(
                        PaymentTransaction, webhook_payout_id
                    ).payout_batch_id,
                    "BATCH-WEBHOOK",
                )
                self.assertEqual(
                    payout_session.get(FormulaRequest, webhook_request_id).status,
                    RequestStatus.APPROVED.value,
                )
                self.assertEqual(
                    payout_session.get(PoolBalance, 1).available_usd,
                    Decimal("25.00"),
                )
            finally:
                payout_session.close()

            create_order.return_value = {"order_id": "ORDER-SIMULATED"}
            capture_order.return_value = {
                "order_status": "COMPLETED",
                "capture_id": "CAPTURE-SIMULATED",
                "capture_status": "COMPLETED",
                "amount": Decimal("20.00"),
                "currency": "USD",
            }
            simulation_funding = client.post(
                "/api/donations/orders", json={"amount": "20.00"}
            )
            self.assertEqual(simulation_funding.status_code, 201)
            simulation_capture = client.post(
                "/api/donations/orders/ORDER-SIMULATED/capture"
            )
            self.assertEqual(simulation_capture.status_code, 200)
            impact_before_simulation = client.get("/api/public/impact").json()

            with patch.dict(os.environ, {"PAYPAL_PAYOUT_SIMULATED": "true"}):
                self.assertEqual(
                    client.get("/api/admin/dashboard", headers=auth).json()[
                        "payout_mode"
                    ],
                    "SIMULATED",
                )
                simulated_submission = client.post(
                    f"/api/admin/approve/{older_infant.json()['id']}",
                    headers=auth,
                )
                self.assertEqual(
                    simulated_submission.status_code,
                    200,
                    simulated_submission.text,
                )
                simulated_body = simulated_submission.json()
                self.assertEqual(simulated_body["payout_mode"], "SIMULATED")
                self.assertEqual(
                    simulated_body["payout_status"], "SIMULATED_PENDING"
                )
                self.assertEqual(
                    simulated_body["lifecycle_status"], "payout_pending"
                )
                self.assertIsNone(simulated_body["pickup_token"])
                self.assertEqual(payout.call_count, 1)
                self.assertEqual(
                    client.post(
                        f"/api/admin/payout/{older_infant.json()['id']}/"
                        "simulate-confirmation"
                    ).status_code,
                    401,
                )
                simulated_confirmation = client.post(
                    f"/api/admin/payout/{older_infant.json()['id']}/"
                    "simulate-confirmation",
                    headers=auth,
                )
                self.assertEqual(
                    simulated_confirmation.status_code,
                    200,
                    simulated_confirmation.text,
                )
                self.assertEqual(
                    simulated_confirmation.json()["lifecycle_status"],
                    "ready_for_pickup",
                )
                self.assertIsNone(
                    simulated_confirmation.json()["pickup_token"]
                )
                impact_after_simulation = client.get("/api/public/impact").json()
                self.assertEqual(
                    impact_after_simulation["families_supported_count"],
                    impact_before_simulation["families_supported_count"],
                )
                self.assertEqual(
                    impact_after_simulation["infants_sustained"],
                    impact_before_simulation["infants_sustained"],
                )
                retrieved_simulated_token = client.post(
                    f"/api/admin/requests/{older_infant.json()['id']}/"
                    "pickup-token/retrieve",
                    headers=auth,
                )
                self.assertEqual(
                    retrieved_simulated_token.status_code,
                    200,
                    retrieved_simulated_token.text,
                )
                self.assertTrue(retrieved_simulated_token.json()["token"])

            incorrect_current_password = client.post(
                "/api/auth/change-password",
                headers=auth,
                json={
                    "current_password": "not-the-current-password",
                    "new_password": "a-new-secure-test-password",
                },
            )
            self.assertEqual(incorrect_current_password.status_code, 403)

            changed_password = client.post(
                "/api/auth/change-password",
                headers=auth,
                json={
                    "current_password": "a-secure-test-password",
                    "new_password": "a-new-secure-test-password",
                },
            )
            self.assertEqual(changed_password.status_code, 200, changed_password.text)
            new_auth = {
                "Authorization": f"Bearer {changed_password.json()['access_token']}"
            }
            self.assertEqual(
                client.get("/api/auth/me", headers=new_auth).status_code, 200
            )
            self.assertEqual(
                client.get("/api/auth/me", headers=auth).status_code, 401
            )
            old_password_login = client.post(
                "/api/auth/login",
                json={
                    "email": "operator@example.test",
                    "password": "a-secure-test-password",
                },
            )
            self.assertEqual(old_password_login.status_code, 401)
            new_password_login = client.post(
                "/api/auth/login",
                json={
                    "email": "operator@example.test",
                    "password": "a-new-secure-test-password",
                },
            )
            self.assertEqual(new_password_login.status_code, 200)

            partial_case = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Partial support case",
                    "phone_number": "+254700000099",
                    "location": "Kibera, Nairobi",
                    "county": "Nairobi County",
                    "infant_age_months": 0,
                },
            )
            self.assertEqual(partial_case.status_code, 201, partial_case.text)
            partial_case_id = partial_case.json()["id"]
            partial_approval = client.post(
                f"/api/admin/requests/{partial_case_id}/decision",
                headers=new_auth,
                json={"decision": "approve"},
            )
            self.assertEqual(partial_approval.status_code, 200, partial_approval.text)
            partial_match = client.post(
                f"/api/admin/requests/{partial_case_id}/match",
                headers=new_auth,
                json={"merchant_id": merchant.json()["id"]},
            )
            self.assertEqual(partial_match.status_code, 200, partial_match.text)
            partial_session = main.SessionLocal()
            try:
                pool = partial_session.get(PoolBalance, 1)
                pool.available_usd = Decimal("15.00")
                partial_session.commit()
            finally:
                partial_session.close()
            full_purchase = client.post(
                f"/api/admin/approve/{partial_case_id}",
                headers=new_auth,
            )
            self.assertEqual(full_purchase.status_code, 409)
            excessive_partial = client.post(
                f"/api/admin/approve/{partial_case_id}",
                headers=new_auth,
                json={"containers": 4},
            )
            self.assertEqual(excessive_partial.status_code, 422)
            partial_payment = client.post(
                f"/api/admin/approve/{partial_case_id}",
                headers=new_auth,
                json={"containers": 1},
            )
            self.assertEqual(partial_payment.status_code, 200, partial_payment.text)
            self.assertEqual(partial_payment.json()["amount"], "15.00")
            self.assertEqual(payout.call_args.kwargs["amount"], Decimal("15.00"))
            studio_partial_payouts = client.get(
                "/api/admin/studio/payouts",
                headers=new_auth,
            )
            self.assertEqual(studio_partial_payouts.status_code, 200)
            reported_partial_payout = next(
                row
                for row in studio_partial_payouts.json()
                if row["request_id"] == partial_case_id
            )
            self.assertEqual(reported_partial_payout["containers"], 1)
            self.assertEqual(reported_partial_payout["amount"], 15.0)
            partial_session = main.SessionLocal()
            try:
                partially_funded_request = partial_session.get(
                    FormulaRequest, partial_case_id
                )
                partial_transaction = (
                    partial_session.query(PaymentTransaction)
                    .filter_by(request_id=partial_case_id)
                    .one()
                )
                self.assertEqual(partially_funded_request.containers_per_month, 3)
                self.assertEqual(partially_funded_request.total_cost_usd, Decimal("45.00"))
                self.assertEqual(partial_transaction.containers, 1)
                self.assertEqual(partial_transaction.amount, Decimal("15.00"))
            finally:
                partial_session.close()

    def test_vendor_and_request_detail_endpoints(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PICKUP_TOKEN_ENCRYPTION_KEY": _test_pickup_token_key,
                },
            ),
            TestClient(main.app) as client,
        ):
            auth = get_valid_admin_auth(client)

            vendor = client.put(
                "/api/admin/vendors/Kibera%20Pharmacy",
                headers=auth,
                json={
                    "name": "Kibera Pharmacy",
                    "location": "Kibera, Nairobi",
                    "county": "Nairobi County",
                    "paypal_email": "merchant@example.test",
                    "price_per_container_usd": "10.00",
                },
            )
            self.assertEqual(vendor.status_code, 200, vendor.text)

            request_payload = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Amina Njeri",
                    "phone_number": "+254700000010",
                    "location": "Kibera",
                    "county": "Nairobi County",
                    "infant_age_months": 4,
                    "formula_needs": "Needs formula for one month",
                },
            )
            self.assertEqual(request_payload.status_code, 201, request_payload.text)
            request_id = request_payload.json()["id"]

            current_month = datetime.now(timezone.utc).date().replace(day=1)
            birth_month = date(current_month.year - 1, current_month.month, 1)
            updated_legacy_request = client.patch(
                f"/api/admin/requests/{request_id}",
                headers=auth,
                json={"birth_month": birth_month.isoformat()},
            )
            self.assertEqual(
                updated_legacy_request.status_code,
                200,
                updated_legacy_request.text,
            )
            self.assertEqual(
                updated_legacy_request.json()["birth_month"],
                birth_month.isoformat(),
            )
            self.assertEqual(
                updated_legacy_request.json()["support_month"],
                current_month.isoformat(),
            )
            self.assertEqual(
                updated_legacy_request.json()["continuity_id"],
                request_id,
            )

            request_detail = client.get(
                f"/api/admin/requests/{request_id}",
                headers=auth,
            )
            self.assertEqual(request_detail.status_code, 200, request_detail.text)
            self.assertEqual(request_detail.json()["recipient_name"], "Amina Njeri")
            self.assertEqual(request_detail.json()["location"], "Kibera")
            self.assertEqual(request_detail.json()["county"], "Nairobi County")
            studio_requests = client.get(
                "/api/admin/studio/requests",
                headers=auth,
            )
            self.assertEqual(studio_requests.status_code, 200, studio_requests.text)
            studio_request = next(
                row for row in studio_requests.json() if row["id"] == request_id
            )
            self.assertEqual(studio_request["county"], "Nairobi County")

            vendor_detail = client.get(
                "/api/admin/vendors/Kibera%20Pharmacy",
                headers=auth,
            )
            self.assertEqual(vendor_detail.status_code, 200, vendor_detail.text)
            self.assertEqual(vendor_detail.json()["name"], "Kibera Pharmacy")
            self.assertEqual(vendor_detail.json()["paypal_email"], "merchant@example.test")
            self.assertEqual(vendor_detail.json()["county"], "Nairobi County")
            studio_merchants = client.get(
                "/api/admin/studio/merchants",
                headers=auth,
            )
            self.assertEqual(studio_merchants.status_code, 200, studio_merchants.text)
            studio_merchant = next(
                row for row in studio_merchants.json() if row["id"] == vendor.json()["id"]
            )
            self.assertEqual(studio_merchant["county"], "Nairobi County")

    def test_admin_management_endpoints(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PICKUP_TOKEN_ENCRYPTION_KEY": _test_pickup_token_key,
                },
            ),
            patch.object(
                main,
                "create_donation_order",
                return_value={"order_id": "ADMIN-MANAGE-ORDER"},
            ),
            patch.object(
                main,
                "capture_donation_order",
                return_value={
                    "order_status": "COMPLETED",
                    "capture_id": "ADMIN-MANAGE-CAPTURE",
                    "capture_status": "COMPLETED",
                    "amount": Decimal("25.00"),
                    "currency": "USD",
                },
            ),
            TestClient(main.app) as client,
        ):
            auth = get_valid_admin_auth(client)

            vendor = client.put(
                "/api/admin/vendors/Delta%20Pharmacy",
                headers=auth,
                json={
                    "name": "Delta Pharmacy",
                    "location": "Nairobi",
                    "county": "Nairobi County",
                    "paypal_email": "delta@example.test",
                    "price_per_container_usd": "12.00",
                },
            )
            self.assertEqual(vendor.status_code, 200, vendor.text)
            self.assertEqual(vendor.json()["county"], "Nairobi County")

            request_payload = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Grace Wambui",
                    "phone_number": "+254700000020",
                    "location": "Nairobi",
                    "county": "Nairobi County",
                    "infant_age_months": 7,
                    "formula_needs": "Needs formula support",
                },
            )
            self.assertEqual(request_payload.status_code, 201, request_payload.text)
            request_id = request_payload.json()["id"]

            edited = client.patch(
                f"/api/admin/requests/{request_id}",
                headers=auth,
                json={
                    "recipient_name": "Grace Wambui Updated",
                    "location": "Eastleigh, Nairobi",
                    "county": "Kiambu County",
                },
            )
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(edited.json()["recipient_name"], "Grace Wambui Updated")
            self.assertEqual(edited.json()["location"], "Eastleigh, Nairobi")
            self.assertEqual(edited.json()["county"], "Kiambu County")

            donation_order = client.post(
                "/api/donations/orders",
                json={"amount": "25.00"},
            )
            self.assertEqual(donation_order.status_code, 201, donation_order.text)
            capture = client.post(
                f"/api/donations/orders/{donation_order.json()['order_id']}/capture"
            )
            self.assertEqual(capture.status_code, 200, capture.text)

            donation_list = client.get("/api/admin/donations", headers=auth)
            self.assertEqual(donation_list.status_code, 200, donation_list.text)
            self.assertEqual(len(donation_list.json()), 1)

            deleted = client.delete(
                "/api/admin/vendors/Delta%20Pharmacy",
                headers=auth,
            )
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(deleted.json()["deleted"], True)

    def test_successful_paypal_refresh_updates_request_and_impact(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PICKUP_TOKEN_ENCRYPTION_KEY": _test_pickup_token_key,
                    "PAYPAL_PAYOUT_SIMULATED": "false",
                },
            ),
            patch.object(
                main,
                "create_donation_order",
                return_value={"order_id": "ORDER-IMMEDIATE-SUCCESS"},
            ),
            patch.object(
                main,
                "capture_donation_order",
                return_value={
                    "order_status": "COMPLETED",
                    "capture_id": "CAPTURE-IMMEDIATE-SUCCESS",
                    "capture_status": "COMPLETED",
                    "amount": Decimal("30.00"),
                    "currency": "USD",
                },
            ),
            patch.object(
                main,
                "create_supplier_payout",
                return_value={
                    "payout_batch_id": "BATCH-IMMEDIATE-SUCCESS",
                    "status": "SUCCESS",
                },
            ),
            patch.object(
                main,
                "get_supplier_payout_status",
                return_value={"status": "SUCCESS"},
            ),
            TestClient(main.app) as client,
        ):
            auth = get_valid_admin_auth(client)
            dashboard_before = client.get(
                "/api/admin/dashboard", headers=auth
            ).json()
            vendor = client.put(
                "/api/admin/vendors/ImmediateSuccessPharmacy",
                headers=auth,
                json={
                    "name": "ImmediateSuccessPharmacy",
                    "location": "Nairobi",
                    "county": "Nairobi County",
                    "paypal_email": "immediate@example.test",
                    "price_per_container_usd": "10.00",
                },
            )
            self.assertEqual(vendor.status_code, 200, vendor.text)

            donation_order = client.post(
                "/api/donations/orders",
                json={"amount": "30.00"},
            )
            self.assertEqual(donation_order.status_code, 201, donation_order.text)
            capture = client.post(
                "/api/donations/orders/ORDER-IMMEDIATE-SUCCESS/capture"
            )
            self.assertEqual(capture.status_code, 200, capture.text)
            families_before_payout = client.get(
                "/api/public/impact"
            ).json()["families_supported_count"]

            request = client.post(
                "/api/requests",
                json={
                    "recipient_name": "Immediate Success Recipient",
                    "phone_number": "+254700000099",
                    "location": "Nairobi",
                    "county": "Nairobi County",
                    "infant_age_months": 2,
                },
            )
            self.assertEqual(request.status_code, 201, request.text)
            request_id = request.json()["id"]
            approved = client.post(
                f"/api/admin/requests/{request_id}/decision",
                headers=auth,
                json={"decision": "approve"},
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            matched = client.post(
                f"/api/admin/requests/{request_id}/match",
                headers=auth,
                json={"merchant_id": vendor.json()["id"]},
            )
            self.assertEqual(matched.status_code, 200, matched.text)

            payout = client.post(
                f"/api/admin/approve/{request_id}",
                headers=auth,
            )
            self.assertEqual(payout.status_code, 200, payout.text)
            self.assertEqual(payout.json()["payout_status"], "SUCCESS")
            self.assertEqual(payout.json()["status"], "Approved")
            self.assertEqual(payout.json()["lifecycle_status"], "payout_pending")
            dashboard_pending = client.get(
                "/api/admin/dashboard", headers=auth
            ).json()
            self.assertEqual(
                Decimal(dashboard_pending["donations_total_usd"]),
                Decimal(dashboard_before["donations_total_usd"])
                + Decimal("30.00"),
            )
            self.assertEqual(
                Decimal(dashboard_pending["paypal_payouts_sent_usd"]),
                Decimal(dashboard_before["paypal_payouts_sent_usd"]),
            )
            self.assertEqual(
                Decimal(dashboard_pending["paypal_payouts_pending_usd"]),
                Decimal(dashboard_before["paypal_payouts_pending_usd"])
                + Decimal("30.00"),
            )
            self.assertEqual(
                client.get("/api/public/impact").json()["families_supported_count"],
                families_before_payout,
            )
            payout_refresh = client.post(
                f"/api/admin/payout/{request_id}/refresh",
                headers=auth,
            )
            self.assertEqual(payout_refresh.status_code, 200, payout_refresh.text)
            self.assertEqual(payout_refresh.json()["payout_status"], "SUCCESS")
            self.assertEqual(payout_refresh.json()["status"], "Paid")
            self.assertEqual(
                payout_refresh.json()["lifecycle_status"],
                "ready_for_pickup",
            )

            request_detail = client.get(
                f"/api/admin/requests/{request_id}",
                headers=auth,
            )
            self.assertEqual(request_detail.status_code, 200, request_detail.text)
            self.assertEqual(request_detail.json()["payout_status"], "SUCCESS")
            self.assertIsNotNone(request_detail.json()["payout_confirmed_at"])

            confirmed_date = request_detail.json()["payout_confirmed_at"][:10]
            supplier_report = client.get(
                "/api/admin/reports/supplier-payouts",
                headers=auth,
                params={
                    "supplier": "ImmediateSuccessPharmacy",
                    "start_date": confirmed_date,
                    "end_date": confirmed_date,
                },
            )
            self.assertEqual(
                supplier_report.status_code, 200, supplier_report.text
            )
            self.assertEqual(
                supplier_report.json()["payment_count"], 1
            )
            self.assertEqual(
                supplier_report.json()["total_amount_usd"], "30.00"
            )
            self.assertEqual(supplier_report.json()["total_containers"], 3)
            self.assertEqual(len(supplier_report.json()["payments"]), 1)

            outside_period = client.get(
                "/api/admin/reports/supplier-payouts",
                headers=auth,
                params={
                    "supplier": "ImmediateSuccessPharmacy",
                    "start_date": "2020-01-01",
                    "end_date": "2020-01-31",
                },
            )
            self.assertEqual(outside_period.status_code, 200)
            self.assertEqual(outside_period.json()["payment_count"], 0)
            invalid_period = client.get(
                "/api/admin/reports/supplier-payouts",
                headers=auth,
                params={
                    "supplier": "ImmediateSuccessPharmacy",
                    "start_date": "2026-11-12",
                    "end_date": "2026-11-11",
                },
            )
            self.assertEqual(invalid_period.status_code, 422)

            dashboard = client.get("/api/admin/dashboard", headers=auth)
            self.assertEqual(dashboard.status_code, 200, dashboard.text)
            self.assertEqual(
                dashboard.json()["families_supported_count"],
                families_before_payout + 1,
            )
            self.assertEqual(
                Decimal(dashboard.json()["paypal_payouts_sent_usd"]),
                Decimal(dashboard_before["paypal_payouts_sent_usd"])
                + Decimal("30.00"),
            )
            self.assertEqual(
                Decimal(dashboard.json()["paypal_payouts_pending_usd"]),
                Decimal(dashboard_before["paypal_payouts_pending_usd"]),
            )
            self.assertTrue(
                any(
                    payout_status["status"] == "SUCCESS"
                    and payout_status["payout_mode"] == "PAYPAL"
                    for payout_status in dashboard.json()["payout_status_counts"]
                )
            )
            impact = client.get("/api/public/impact")
            self.assertEqual(
                impact.json()["families_supported_count"],
                families_before_payout + 1,
            )
            self.assertEqual(
                impact.json()["infants_sustained"],
                families_before_payout + 1,
            )

            studio_payouts = client.get(
                "/api/admin/studio/payouts",
                headers=auth,
            )
            self.assertTrue(
                any(
                    row["request_id"] == request_id and row["status"] == "SUCCESS"
                    for row in studio_payouts.json()
                )
            )
            studio_requests = client.get(
                "/api/admin/studio/requests",
                headers=auth,
            )
            self.assertTrue(
                any(
                    row["id"] == request_id
                    and row["lifecycle_status"] == "ready_for_pickup"
                    and row["payout_confirmed_at"] is not None
                    for row in studio_requests.json()
                )
            )


if __name__ == "__main__":
    unittest.main()
