"""Seed 22 realistic failed-payment events, at least two per failure class.

Each event carries Razorpay's error object verbatim ({code, description, source, step,
reason, metadata}) and an `expected_class`: the ground truth the classifier tests assert
against and the label the simulation uses. Three events are UNKNOWN on purpose: real
issuers emit free-text (sometimes mixed-language) messages no rule should pretend to
understand; those are the LLM's job, and failing that, a person's.

Usage:  python scripts/seed.py        (idempotent: re-running inserts nothing new)

The two LIMIT_EXCEEDED events sit last so that the per-class index of every earlier
event (what tests and the chaos harness look events up by) is unchanged.
"""
import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # importable from a clean clone, any cwd

from sqlalchemy import select  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.models import PaymentAttempt, audit  # noqa: E402
from app.taxonomy import FailureClass  # noqa: E402

BAD_REQUEST, GATEWAY, SERVER = "BAD_REQUEST_ERROR", "GATEWAY_ERROR", "SERVER_ERROR"


def _event(payment_id, order_id, merchant, name, contact, email, amount_paise, method, has_token,
           minutes_ago, expected, *, code, source, step, reason, description, metadata=None, language=None) -> dict:
    assert not has_token or method in ("card", "emandate"), payment_id  # tokens only exist for cards and mandates
    return {
        "razorpay_payment_id": payment_id, "order_id": order_id, "merchant_id": merchant,
        "customer_name": name, "customer_contact": contact, "customer_email": email,
        "amount_paise": amount_paise, "currency": "INR", "method": method, "has_token": has_token,
        "failed_minutes_ago": minutes_ago,
        "customer_language": language,  # en | hi | hinglish | None (NUDGE_LANGUAGE_DEFAULT applies)
        "error": {"code": code, "description": description, "source": source, "step": step,
                  "reason": reason, "metadata": metadata or {}},
        "expected_class": expected,
    }


SEED_EVENTS: list[dict] = [
    # INSUFFICIENT_FUNDS x3
    _event("pay_LrfTLGu5EgYUPo", "order_ct86nidKocRa56", "merchant_acme", "Priya Sharma", "+915876543210",
           "priya.sharma@example.com", 249900, "card", True, 35, FailureClass.INSUFFICIENT_FUNDS,
           code=BAD_REQUEST, source="customer", step="payment_authorization", reason="payment_failed",
           description="Your payment could not be completed due to insufficient funds in the account."),
    _event("pay_3nLRLkYnPv2Q7Y", "order_LcgWz0OdUZQshB", "merchant_bolt", "Rahul Verma", "+915812345678",
           "rahul.verma@example.com", 59900, "upi", False, 42, FailureClass.INSUFFICIENT_FUNDS,
           code=BAD_REQUEST, source="customer", step="payment_authorization", reason="payment_failed",
           description="Transaction failed as the account has insufficient balance. Please add money and retry.",
           metadata={"vpa": "rahul.verma@okhdfcbank"}, language="hi"),
    _event("pay_LbKcdHygUdlGmL", "order_JfrBJnGecoz3JX", "merchant_zenith", "Ananya Iyer", "+915845012345",
           "ananya.iyer@example.com", 1299900, "emandate", True, 180, FailureClass.INSUFFICIENT_FUNDS,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="payment_failed",
           description="Debit failed: insufficient funds in the account (NACH return code 01).",
           metadata={"umrn": "HDFC7000000012345678"}),
    # ISSUER_DOWN x3
    _event("pay_u3uVUGz7clgqP7", "order_THnH0G3rFkE1CM", "merchant_acme", "Vikram Singh", "+915898989898",
           "vikram.singh@example.com", 149900, "card", True, 12, FailureClass.ISSUER_DOWN,
           code=GATEWAY, source="bank", step="payment_authorization", reason="bank_technical_error",
           description="The bank is facing a technical issue; please retry after some time."),
    _event("pay_1F16zybf0bJL98", "order_9yY2yXfJQzDKP5", "merchant_bolt", "Sneha Patel", "+915723456789",
           "sneha.patel@example.com", 349900, "netbanking", False, 18, FailureClass.ISSUER_DOWN,
           code=GATEWAY, source="bank", step="payment_initiation", reason="payment_failed",
           description="Net banking for the selected bank is currently unavailable due to scheduled maintenance.",
           metadata={"bank": "HDFC"}),
    _event("pay_s3QKA48mtO5AcI", "order_j1onehd2Udf0bs", "merchant_zenith", "Arjun Nair", "+915447123456",
           "arjun.nair@example.com", 79900, "card", False, 25, FailureClass.ISSUER_DOWN,
           code=GATEWAY, source="bank", step="payment_authorization", reason="bank_technical_error",
           description="Issuer bank is down. Transaction could not be processed."),
    # AUTH_ABANDONED x4
    _event("pay_96hhPI3tuHgqH7", "order_K3fszNT01IWIB9", "merchant_acme", "Kavya Reddy", "+915440012345",
           "kavya.reddy@example.com", 199900, "card", False, 8, FailureClass.AUTH_ABANDONED,
           code=BAD_REQUEST, source="customer", step="payment_authentication", reason="payment_cancelled",
           description="Payment was cancelled by the customer on the OTP page."),
    _event("pay_jRGrfsvph4olHV", "order_X1bzCXgdkwUeKi", "merchant_bolt", "Rohan Mehta", "+915820098200",
           "rohan.mehta@example.com", 899900, "card", True, 15, FailureClass.AUTH_ABANDONED,
           code=BAD_REQUEST, source="customer", step="payment_authentication", reason="payment_timed_out",
           description="3DS authentication was not completed in time."),
    _event("pay_1H6Nz6EQnaUhIY", "order_b4MpQR4TBEzgw6", "merchant_zenith", "Divya Krishnan", "+915566012345",
           "divya.krishnan@example.com", 45900, "upi", False, 6, FailureClass.AUTH_ABANDONED,
           code=BAD_REQUEST, source="customer", step="payment_authentication", reason="payment_timed_out",
           description="UPI collect request expired: the customer did not approve the payment in the UPI app.",
           metadata={"vpa": "divya.k@ybl"}, language="hinglish"),
    _event("pay_vKQKr2NhpyjKrj", "order_PFVVNB3BaGzXGo", "merchant_acme", "Aditya Joshi", "+915011223344",
           "aditya.joshi@example.com", 2499900, "netbanking", False, 22, FailureClass.AUTH_ABANDONED,
           code=BAD_REQUEST, source="customer", step="payment_authentication", reason="payment_cancelled",
           description="Customer closed the bank login page before completing authentication.",
           metadata={"bank": "ICIC"}),
    # HARD_DECLINE x3
    _event("pay_WbVjoy6lWGjt3K", "order_NOMFWfRRzcY5Pu", "merchant_bolt", "Neha Gupta", "+915910011223",
           "neha.gupta@example.com", 129900, "card", False, 50, FailureClass.HARD_DECLINE,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="card_declined",
           description="Card declined: the card has expired."),
    _event("pay_GXCEDfgfRgYqbG", "order_XJKcENIlSwJ3jT", "merchant_zenith", "Karthik Rao", "+915886655443",
           "karthik.rao@example.com", 39900, "card", True, 65, FailureClass.HARD_DECLINE,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="card_declined",
           description="Your card is blocked for online transactions. Please contact your bank."),
    _event("pay_KJWsusMa3fuDGy", "order_TCwdQbDNLiuuic", "merchant_acme", "Pooja Desai", "+915725544332",
           "pooja.desai@example.com", 549900, "card", False, 90, FailureClass.HARD_DECLINE,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="card_declined",
           description="Card not enabled for international transactions.", language="hi"),
    # RISK_BLOCKED x2
    _event("pay_7wMfZywsQzhW8J", "order_lx4AhVf5t0Tc8q", "merchant_bolt", "Siddharth Menon", "+915495123456",
           "siddharth.menon@example.com", 1999900, "card", True, 30, FailureClass.RISK_BLOCKED,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="payment_risk_check_failed",
           description="Transaction flagged as suspicious by the issuing bank's risk engine."),
    _event("pay_KCPQqKpOy2DAjS", "order_VnOACmA5ozBEBQ", "merchant_zenith", "Meera Pillai", "+915847098470",
           "meera.pillai@example.com", 24900, "wallet", False, 45, FailureClass.RISK_BLOCKED,
           code=BAD_REQUEST, source="business", step="payment_authorization", reason="payment_failed",
           description="Payment declined: suspected fraud on this account.",
           metadata={"wallet": "phonepe"}),
    # NETWORK_TIMEOUT x2
    _event("pay_TpawZfwUkh1lUW", "order_BhnkWpXcehNNwQ", "merchant_acme", "Aman Chauhan", "+915650012345",
           "aman.chauhan@example.com", 99900, "card", True, 4, FailureClass.NETWORK_TIMEOUT,
           code=GATEWAY, source="gateway", step="payment_authorization", reason="payment_timed_out",
           description="Gateway timeout while waiting for the issuer's authorization response."),
    _event("pay_RYkl7zDTS9p5l9", "order_QpHOuHwUDRtKwC", "merchant_bolt", "Ishita Banerjee", "+915830098300",
           "ishita.banerjee@example.com", 19900, "upi", False, 9, FailureClass.NETWORK_TIMEOUT,
           code=GATEWAY, source="network", step="payment_authorization", reason="payment_failed",
           description="Request timed out at the NPCI switch.",
           metadata={"vpa": "ishita.b@paytm"}),
    # UNKNOWN x3: free text no rule should pretend to understand
    _event("pay_MIV28IGbxvuS9s", "order_AgZDnKaqJgRiKB", "merchant_zenith", "Varun Kulkarni", "+915922334455",
           "varun.kulkarni@example.com", 69900, "card", False, 55, FailureClass.UNKNOWN,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="card_declined",
           description="Payment was declined by the issuing bank."),
    _event("pay_Roh45VYHOI5FWf", "order_huNiXebpovaWDG", "merchant_acme", "Nandini Bhat", "+915845678901",
           "nandini.bhat@example.com", 159900, "netbanking", False, 75, FailureClass.UNKNOWN,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="payment_failed",
           description="Aapka transaction bank dwara reject kar diya gaya hai. Kripya apne bank se sampark karein. (Ref: SBIN-91)",
           metadata={"bank": "SBIN"}),
    _event("pay_WiFGRDkkn0jja0", "order_k9xiIlq4rmTx1R", "merchant_bolt", "Farhan Sheikh", "+915711122233",
           "farhan.sheikh@example.com", 449900, "card", True, 120, FailureClass.UNKNOWN,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="payment_failed",
           description="Transaction declined by the issuer. Response code 05: Do not honour."),
    # LIMIT_EXCEEDED x2
    _event("pay_Qm2VxT8hKp1LnE", "order_Zr7cWq4NbY3sHf", "merchant_zenith", "Ritika Malhotra", "+915933445566",
           "ritika.malhotra@example.com", 1099900, "upi", False, 40, FailureClass.LIMIT_EXCEEDED,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="payment_failed",
           description="Daily UPI transaction limit exceeded",
           metadata={"vpa": "ritika.m@oksbi"}),
    _event("pay_Hs6DwN3yRj9TcA", "order_Fk1pLm8VeX5qGd", "merchant_acme", "Manish Tiwari", "+915877665544",
           "manish.tiwari@example.com", 2299900, "card", True, 70, FailureClass.LIMIT_EXCEEDED,
           code=BAD_REQUEST, source="bank", step="payment_authorization", reason="card_declined",
           description="61 Exceeds withdrawal amount limit"),
]


def to_attempt(event: dict, now=None) -> PaymentAttempt:
    """Build an unsaved PaymentAttempt row from a seed event (tests and the simulation use this without a DB)."""
    now = now or utcnow()
    err = event["error"]
    return PaymentAttempt(
        merchant_id=event["merchant_id"], order_id=event["order_id"], razorpay_payment_id=event["razorpay_payment_id"],
        amount_paise=event["amount_paise"], currency=event["currency"], method=event["method"],
        error_code=err["code"], error_source=err["source"], error_step=err["step"], error_reason=err["reason"],
        error_description=err["description"], has_token=event["has_token"],
        customer_name=event["customer_name"], customer_contact=event["customer_contact"],
        customer_email=event["customer_email"], customer_language=event.get("customer_language"),
        failed_at=now - timedelta(minutes=event["failed_minutes_ago"]),
    )


def existing_ids(session) -> set[str]:
    ids = [e["razorpay_payment_id"] for e in SEED_EVENTS]
    rows = session.execute(select(PaymentAttempt.razorpay_payment_id)
                           .where(PaymentAttempt.razorpay_payment_id.in_(ids))).all()
    return {r[0] for r in rows}


def seed(session, now=None) -> list[PaymentAttempt]:
    """Insert every seed event not already present; commit; return the rows for all 22 in SEED_EVENTS order."""
    now = now or utcnow()
    present = existing_ids(session)
    for event in SEED_EVENTS:
        if event["razorpay_payment_id"] in present:
            continue
        row = to_attempt(event, now)
        session.add(row)
        session.flush()  # need row.id for the audit line; the same transaction commits both below
        audit(session, row.id, "ingest", "failed payment event received",
              {"razorpay_payment_id": row.razorpay_payment_id, "method": row.method, "amount_paise": row.amount_paise,
               "error": event["error"], "has_token": row.has_token})
    session.commit()
    rows = session.execute(select(PaymentAttempt).where(
        PaymentAttempt.razorpay_payment_id.in_([e["razorpay_payment_id"] for e in SEED_EVENTS]))).scalars().all()
    by_id = {r.razorpay_payment_id: r for r in rows}
    return [by_id[e["razorpay_payment_id"]] for e in SEED_EVENTS]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python scripts/seed.py",
                            description="insert the 22 seed failed-payment events (idempotent)").parse_args(argv)
    from app import db
    db.init_db()
    s = db.session()
    try:
        before = len(existing_ids(s))
        rows = seed(s)
        print(f"seeded {len(rows) - before} events ({before} already present)")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
