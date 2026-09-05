"""Razorpay Payment Links over stdlib urllib, plus the fixture and fault-injecting clients.

Why not the official SDK: it collapses every non-2xx into ServerError/BadRequestError
carrying only a message. The executor's backoff policy keys on the HTTP status (429 and
5xx retry with backoff; any other 4xx is final), so the status has to survive.

Three clients share one Protocol so the executor never knows which it is talking to:
  LiveRazorpayClient      real HTTPS, test-mode keys only
  FixtureRazorpayClient   in-memory, deterministic, no network (the default without keys)
  FaultingRazorpayClient  wraps another client to inject 429 / 5xx for the chaos harness
"""
import base64
import hashlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from . import config, faults
from .clock import to_unix, utcnow

API_BASE_URL = "https://api.razorpay.com/v1"
HTTP_TIMEOUT_SECONDS = 20.0
USER_AGENT = "payment-recovery-agent/0.1"


class RazorpayError(Exception):
    """A non-2xx answer (status = HTTP status) or a transport failure (status = 0, code = NETWORK)."""

    def __init__(self, status: int, code: str, description: str, details: dict | None = None):
        self.status = int(status)
        self.code = code or "UNKNOWN_ERROR"
        self.description = description or ""
        self.details = details or {}
        super().__init__(str(self))

    def __str__(self) -> str:
        where = f"HTTP {self.status}" if self.status else "network"
        return f"Razorpay {where} {self.code}: {self.description}"


class RazorpayClient(Protocol):
    name: str

    def create_payment_link(self, payload: dict) -> dict: ...

    def fetch_payment_link(self, link_id: str) -> dict: ...

    def list_payment_links(self, *, reference_id: str) -> list[dict]: ...

    def cancel_payment_link(self, link_id: str) -> dict: ...

    def list_order_payments(self, order_id: str) -> list[dict]: ...

    def notify_payment_link(self, link_id: str, medium: str) -> dict: ...


NOTIFY_MEDIA = ("sms", "email")


def _check_medium(medium: str) -> str:
    if medium not in NOTIFY_MEDIA:
        raise ValueError(f"notify medium must be one of {NOTIFY_MEDIA}, got {medium!r}")
    return medium


class LiveRazorpayClient:
    """Real API. Construction refuses anything but rzp_test_ keys: this agent never touches live money."""

    name = "live"

    def __init__(self, key_id: str, key_secret: str, *, base_url: str = API_BASE_URL,
                 timeout: float = HTTP_TIMEOUT_SECONDS):
        if not key_id or not key_id.startswith("rzp_test_"):
            raise RuntimeError("LiveRazorpayClient accepts test-mode keys only (rzp_test_...); refusing to start.")
        if not key_secret:
            raise RuntimeError("RAZORPAY_KEY_SECRET is empty.")
        token = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
        self._auth_header = f"Basic {token}"
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # never echo the secret
        return f"LiveRazorpayClient(base_url={self._base_url!r})"

    def create_payment_link(self, payload: dict) -> dict:
        return self._request("POST", "/payment_links", payload)

    def fetch_payment_link(self, link_id: str) -> dict:
        return self._request("GET", f"/payment_links/{link_id}")

    def list_payment_links(self, *, reference_id: str) -> list[dict]:
        """GET /payment_links?reference_id=... -> the links created under that reference.

        ASSUMED shape (docs unreachable offline): the "fetch all" endpoint filters by
        reference_id and answers {"payment_links": [...]}. The executor only uses this to
        reconcile after an ambiguous failure, and treats any error here as "unknown", which
        parks the job for a person rather than minting a second link."""
        query = urllib.parse.urlencode({"reference_id": reference_id})
        doc = self._request("GET", f"/payment_links?{query}")
        items = doc.get("payment_links")
        if items is None:
            items = doc.get("items", [])
        return [x for x in items if isinstance(x, dict) and x.get("reference_id") == reference_id]

    def cancel_payment_link(self, link_id: str) -> dict:
        """POST /payment_links/{id}/cancel -> the link entity with status "cancelled".

        ASSUMED shape (docs unreachable offline): the documented cancel endpoint takes no body
        and answers the updated link. The ingest path calls this when the order behind a live
        link was paid another way; any error here parks the payment for a person, so a wrong
        assumption costs one review, never a silent live link."""
        return self._request("POST", f"/payment_links/{link_id}/cancel", {})

    def list_order_payments(self, order_id: str) -> list[dict]:
        """GET /orders/{id}/payments -> the payments made against that order.

        ASSUMED shape: {"entity": "collection", "count": n, "items": [payment entities with a
        "status"]}. The executor treats any item with status "captured" as the order being paid
        and creates nothing; a failure of the call itself is audited and the send proceeds (the
        payment.captured webhook is the primary record, this check is belt and braces)."""
        doc = self._request("GET", f"/orders/{order_id}/payments")
        items = doc.get("items")
        if items is None:
            items = doc.get("payments", [])
        return [x for x in items if isinstance(x, dict)]

    def notify_payment_link(self, link_id: str, medium: str) -> dict:
        """POST /payment_links/{id}/notify_by/{sms|email} -> {"success": true}.

        ASSUMED shape (docs unreachable offline; the endpoint is the documented "send/resend
        notification" call): no body, and a 2xx answers {"success": true}. Razorpay sends ITS OWN
        link message on that channel; the agent's drafted nudge wording is not what goes out.
        The response is stored verbatim as the delivery receipt (app/cadence.py); any non-2xx
        raises RazorpayError and the reminder job fails without touching the link."""
        return self._request("POST", f"/payment_links/{link_id}/notify_by/{_check_medium(medium)}", {})

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read()
        except urllib.error.HTTPError as exc:  # must precede URLError/OSError: it subclasses both
            raise _error_from_http(exc) from None
        except (OSError, http.client.HTTPException) as exc:  # URLError, timeouts, resets, TLS, bad status lines
            reason = getattr(exc, "reason", None) or exc
            raise RazorpayError(0, "NETWORK", f"{type(exc).__name__}: {reason}") from None
        return _parse_body(raw, status)


def _error_from_http(exc: urllib.error.HTTPError) -> RazorpayError:
    status = int(getattr(exc, "code", 0) or 0)
    try:
        raw = exc.read()
    except Exception:  # a body we cannot read is still an error we can report
        raw = b""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        err = doc["error"]
        return RazorpayError(status, str(err.get("code") or f"HTTP_{status}"),
                             str(err.get("description") or exc.reason or ""), details=err)
    # tolerate HTML / empty bodies from proxies and load balancers
    return RazorpayError(status, f"HTTP_{status}", f"non-JSON error body: {text[:200].strip() or exc.reason}")


def _parse_body(raw: bytes, status: int) -> dict:
    text = raw.decode("utf-8", errors="replace") if raw else ""
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError:
        raise RazorpayError(status, "BAD_RESPONSE", f"non-JSON 2xx body: {text[:200].strip()}") from None
    if not isinstance(doc, dict):
        raise RazorpayError(status, "BAD_RESPONSE", "2xx body is not a JSON object")
    return doc


_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _b62(seed: str, length: int) -> str:
    n = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest(), "big")
    out = []
    while len(out) < length:
        n, r = divmod(n, len(_ALPHABET))
        out.append(_ALPHABET[r])
    return "".join(out)


class FixtureRazorpayClient:
    """A FIXTURE, not Razorpay. In-memory Payment Links with the request/response shapes we know
    from the API docs, so the executor exercises the exact same code path with or without keys.

    What is faithful: field names and types of the create/fetch responses, ids shaped like
    plink_XXXXXXXXXXXXXX, short_url under https://rzp.io/i/, status lifecycle created -> paid.
    What is ASSUMED and could not be verified offline (docs unreachable from the build box):
      - a second create with the same reference_id is rejected with 400 BAD_REQUEST_ERROR
        "Payment Link with reference_id ... already exists". The executor relies on this only
        as a second line of defence behind the DB UNIQUE constraint, so if the real API instead
        accepts duplicates the worst case is a redundant link, never a double charge.
      - a fetch of an unknown id is a 400 BAD_REQUEST_ERROR ("does not exist").
      - GET /payment_links?reference_id=... lists the links under that reference (the executor's
        reconcile step after an ambiguous failure); an empty list means none was created.
      - the minimal validation below (positive integer amount, 3-letter currency) mirrors the
        real API's, but the real API validates far more.
      - POST /payment_links/{id}/cancel answers the link with status "cancelled" and cancelled_at
        set; cancelling a paid link is a 400 ("only a created link can be cancelled"), cancelling
        a cancelled one is idempotent; an unknown id is a 400 "does not exist".
      - GET /orders/{order_id}/payments answers {"items": [payment entities]}; the fixture answers
        from its own memory (mark_order_paid), never from the links it holds: a recovery link
        creates its own order at Razorpay, so a paid link is not a payment on the original order.
      - accept_partial (bool) and first_min_partial_amount (paise) are valid on a standard link and
        are echoed back; a partial payment leaves the link "partially_paid" with amount_paid set
        (mark_paid). options.checkout.method (per-method booleans) is accepted and echoed under
        "options" (app/offers.py; the real API's validation of both is unverified).
      - POST /payment_links/{id}/notify_by/{sms|email} takes no body and answers {"success": true};
        Razorpay then sends its own link message on that channel. The fixture records the call
        under notifications[link_id] and answers success; notifying an unknown id is a 400 "does
        not exist", a medium other than sms/email is a 400 BAD_REQUEST_ERROR, and notifying a
        paid/expired/cancelled link is a 400 (assumed: Razorpay refuses to re-send a closed link).
    Ids are derived deterministically from reference_id so reruns are reproducible.
    """

    name = "fixture"

    def __init__(self):
        self._links: dict[str, dict] = {}
        self._by_reference: dict[str, str] = {}
        self._order_payments: dict[str, list[dict]] = {}
        self.notifications: dict[str, list[dict]] = {}   # link_id -> [{"medium": ..., "at": unix}, ...]
        self.calls: list[tuple[str, dict]] = []

    def create_payment_link(self, payload: dict) -> dict:
        self.calls.append(("create_payment_link", dict(payload)))
        amount = payload.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", "amount must be a positive integer in paise")
        currency = payload.get("currency") or "INR"
        if not isinstance(currency, str) or len(currency) != 3:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", "currency is invalid")
        reference_id = payload.get("reference_id")
        if reference_id is not None and not isinstance(reference_id, str):
            raise RazorpayError(400, "BAD_REQUEST_ERROR", "reference_id must be a string")
        if reference_id and len(reference_id) > 40:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", "reference_id must be at most 40 characters")
        if reference_id and reference_id in self._by_reference:
            raise RazorpayError(400, "BAD_REQUEST_ERROR",
                                f"Payment Link with reference_id {reference_id} already exists")
        seed = reference_id or json.dumps(payload, sort_keys=True, default=str) + f"#{len(self._links)}"
        link_id = "plink_" + _b62("plink:" + seed, 14)
        now_ts = to_unix(utcnow())
        link = {
            "id": link_id,
            "entity": "payment_link",
            "status": "created",
            "amount": amount,
            "amount_paid": 0,
            "currency": currency,
            "accept_partial": bool(payload.get("accept_partial", False)),
            "first_min_partial_amount": payload.get("first_min_partial_amount"),
            "options": payload.get("options"),
            "description": payload.get("description"),
            "reference_id": reference_id,
            "customer": dict(payload.get("customer") or {}),
            "notify": {"sms": bool((payload.get("notify") or {}).get("sms")),
                       "email": bool((payload.get("notify") or {}).get("email"))},
            "reminder_enable": bool(payload.get("reminder_enable", False)),
            "expire_by": payload.get("expire_by"),
            "notes": dict(payload.get("notes") or {}),
            "callback_url": payload.get("callback_url"),
            "callback_method": payload.get("callback_method"),
            "short_url": "https://rzp.io/i/" + _b62("short:" + seed, 8),
            "payments": [],
            "created_at": now_ts,
            "updated_at": now_ts,
        }
        self._links[link_id] = link
        if reference_id:
            self._by_reference[reference_id] = link_id
        return dict(link)

    def fetch_payment_link(self, link_id: str) -> dict:
        self.calls.append(("fetch_payment_link", {"id": link_id}))
        link = self._links.get(link_id)
        if link is None:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", f"The id provided does not exist: {link_id}")
        return dict(link)

    def mark_paid(self, link_id: str, amount_paid: int | None = None, *, now=None) -> dict:
        """Simulate the customer paying (the demo/poll closes the loop with this)."""
        link = self._links.get(link_id)
        if link is None:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", f"The id provided does not exist: {link_id}")
        paid = int(amount_paid if amount_paid is not None else link["amount"])
        link["amount_paid"] = paid
        link["status"] = "paid" if paid >= link["amount"] else "partially_paid"
        link["updated_at"] = to_unix(now or utcnow())
        return dict(link)

    def list_payment_links(self, *, reference_id: str) -> list[dict]:
        self.calls.append(("list_payment_links", {"reference_id": reference_id}))
        link_id = self._by_reference.get(reference_id)
        return [dict(self._links[link_id])] if link_id else []

    def cancel_payment_link(self, link_id: str) -> dict:
        """POST /payment_links/{id}/cancel. ASSUMED: answers the link with status "cancelled"."""
        self.calls.append(("cancel_payment_link", {"id": link_id}))
        link = self._links.get(link_id)
        if link is None:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", f"The id provided does not exist: {link_id}")
        if link["status"] in ("paid", "partially_paid", "expired"):
            raise RazorpayError(400, "BAD_REQUEST_ERROR",
                                f"Payment link {link_id} is {link['status']}; only a created link can be cancelled")
        if link["status"] != "cancelled":
            link["status"] = "cancelled"
            link["cancelled_at"] = link["updated_at"] = to_unix(utcnow())
        return dict(link)

    def mark_order_paid(self, order_id: str, payment_id: str | None = None, amount: int | None = None, *,
                        now=None) -> dict:
        """Simulate the customer paying the ORIGINAL order some other way (a checkout retry): from
        now on list_order_payments(order_id) answers with this captured payment."""
        pid = payment_id or ("pay_" + _b62("elsewhere:" + order_id, 14))
        payment = {"id": pid, "entity": "payment", "order_id": order_id, "status": "captured", "captured": True,
                   "amount": int(amount) if amount is not None else None, "currency": "INR",
                   "created_at": to_unix(now or utcnow())}
        self._order_payments.setdefault(order_id, []).append(payment)
        return dict(payment)

    def list_order_payments(self, order_id: str) -> list[dict]:
        """GET /orders/{id}/payments. ASSUMED: {"items": [...]}; the fixture returns the list."""
        self.calls.append(("list_order_payments", {"order_id": order_id}))
        return [dict(p) for p in self._order_payments.get(order_id, [])]

    def notify_payment_link(self, link_id: str, medium: str) -> dict:
        """POST /payment_links/{id}/notify_by/{medium}. ASSUMED: answers {"success": true}."""
        self.calls.append(("notify_payment_link", {"id": link_id, "medium": medium}))
        if medium not in NOTIFY_MEDIA:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", f"medium must be one of {', '.join(NOTIFY_MEDIA)}, got {medium!r}")
        link = self._links.get(link_id)
        if link is None:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", f"The id provided does not exist: {link_id}")
        if link["status"] in ("paid", "expired", "cancelled"):
            raise RazorpayError(400, "BAD_REQUEST_ERROR",
                                f"Payment link {link_id} is {link['status']}; notifications can only be sent for an open link")
        self.notifications.setdefault(link_id, []).append({"medium": medium, "at": to_unix(utcnow())})
        return {"success": True}

    def mark_expired(self, link_id: str, *, now=None) -> dict:
        """Simulate the link lapsing unpaid (tests and the demo; the real API expires it at expire_by)."""
        link = self._links.get(link_id)
        if link is None:
            raise RazorpayError(400, "BAD_REQUEST_ERROR", f"The id provided does not exist: {link_id}")
        if link["status"] == "created":
            link["status"] = "expired"
            link["expired_at"] = link["updated_at"] = to_unix(now or utcnow())
        return dict(link)

    def links(self) -> list[dict]:
        return [dict(v) for v in self._links.values()]

    def reset(self) -> None:
        self._links.clear()
        self._by_reference.clear()
        self._order_payments.clear()
        self.notifications.clear()
        self.calls.clear()


class FaultingRazorpayClient:
    """Injects the two Razorpay faults from faults.py at the client boundary.
    razorpay_429: the first two create calls are rate-limited, then the inner client answers.
    razorpay_5xx: every create call is a 502; fetches, lookups, cancels and notifications always pass through."""

    def __init__(self, inner, fault: str):
        if fault not in ("razorpay_429", "razorpay_5xx"):
            raise ValueError(f"FaultingRazorpayClient does not know fault {fault!r}")
        self.inner = inner
        self.fault = fault
        self.name = f"fault:{fault}"
        self.create_calls = 0
        self.fetch_calls = 0
        self.cancel_calls = 0
        self.notify_calls = 0

    def create_payment_link(self, payload: dict) -> dict:
        self.create_calls += 1
        if self.fault == "razorpay_5xx":
            raise RazorpayError(502, "SERVER_ERROR", "Gateway is down")
        if self.fault == "razorpay_429" and self.create_calls <= 2:
            raise RazorpayError(429, "BAD_REQUEST_ERROR", "Too many requests")
        return self.inner.create_payment_link(payload)

    def fetch_payment_link(self, link_id: str) -> dict:
        self.fetch_calls += 1
        return self.inner.fetch_payment_link(link_id)

    def list_payment_links(self, *, reference_id: str) -> list[dict]:
        self.fetch_calls += 1
        return self.inner.list_payment_links(reference_id=reference_id)

    def cancel_payment_link(self, link_id: str) -> dict:
        self.cancel_calls += 1
        return self.inner.cancel_payment_link(link_id)

    def list_order_payments(self, order_id: str) -> list[dict]:
        self.fetch_calls += 1
        return self.inner.list_order_payments(order_id)

    def notify_payment_link(self, link_id: str, medium: str) -> dict:
        self.notify_calls += 1
        return self.inner.notify_payment_link(link_id, medium)


_fixture: FixtureRazorpayClient | None = None


def fixture() -> FixtureRazorpayClient:
    """Process-wide fixture 'server' so links created earlier in a run can be fetched/marked paid later."""
    global _fixture
    if _fixture is None:
        _fixture = FixtureRazorpayClient()
    return _fixture


def reset_fixture() -> None:
    global _fixture
    _fixture = None


def get_client() -> RazorpayClient:
    """Fault wrappers take precedence over keys so the chaos harness never hits the network.
    A fresh wrapper per call: the 429 counter is per client instance, so hold the instance
    for the duration of a run if you want 'two 429s then success' across jobs."""
    if faults.is_active("razorpay_5xx"):
        return FaultingRazorpayClient(fixture(), "razorpay_5xx")
    if faults.is_active("razorpay_429"):
        return FaultingRazorpayClient(fixture(), "razorpay_429")
    if config.razorpay_live():
        return LiveRazorpayClient(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET)
    return fixture()
