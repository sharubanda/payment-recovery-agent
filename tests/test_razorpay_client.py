"""The Razorpay boundary: fixture realism, fault wrappers, and the live client's error handling
(exercised against a fake urlopen; the network is never touched)."""
import base64
import io
import json
import urllib.error
import urllib.request

import pytest

from app import faults
from app import razorpay_client as rc
from app.razorpay_client import (FaultingRazorpayClient, FixtureRazorpayClient, LiveRazorpayClient,
                                 RazorpayError, get_client, reset_fixture)


def _payload(ref="ref-0001", **extra):
    base = {"amount": 49900, "currency": "INR", "description": "Retry payment for order order_1",
            "reference_id": ref, "customer": {"name": "Asha", "contact": "+919999999999", "email": "a@x.io"},
            "notify": {"sms": True, "email": True}, "reminder_enable": True, "expire_by": 1_900_000_000,
            "notes": {"agent": "payment-recovery-agent"}}
    base.update(extra)
    return base


def test_error_str_and_fields():
    err = RazorpayError(429, "BAD_REQUEST_ERROR", "Too many requests")
    assert (err.status, err.code, err.description) == (429, "BAD_REQUEST_ERROR", "Too many requests")
    assert "429" in str(err) and "Too many requests" in str(err)
    assert "network" in str(RazorpayError(0, "NETWORK", "timed out"))


def test_fixture_create_shape_is_deterministic():
    a, b = FixtureRazorpayClient(), FixtureRazorpayClient()
    ra, rb = a.create_payment_link(_payload()), b.create_payment_link(_payload())
    assert ra["id"] == rb["id"] and ra["short_url"] == rb["short_url"]
    assert ra["id"].startswith("plink_") and len(ra["id"]) == len("plink_") + 14
    assert ra["short_url"].startswith("https://rzp.io/i/") and len(ra["short_url"]) == len("https://rzp.io/i/") + 8
    assert ra["status"] == "created" and ra["amount"] == 49900 and ra["amount_paid"] == 0
    assert ra["currency"] == "INR" and ra["reference_id"] == "ref-0001"
    assert ra["customer"]["email"] == "a@x.io" and ra["notify"] == {"sms": True, "email": True}
    assert ra["notes"] == {"agent": "payment-recovery-agent"} and ra["expire_by"] == 1_900_000_000
    assert isinstance(ra["created_at"], int)
    assert a.calls == [("create_payment_link", _payload())]
    # different reference -> different ids
    assert a.create_payment_link(_payload(ref="ref-0002"))["id"] != ra["id"]


def test_fixture_rejects_duplicate_reference_id():
    fx = FixtureRazorpayClient()
    fx.create_payment_link(_payload())
    with pytest.raises(RazorpayError) as info:
        fx.create_payment_link(_payload())
    assert info.value.status == 400 and info.value.code == "BAD_REQUEST_ERROR"
    assert "reference_id" in info.value.description and "already exists" in info.value.description
    assert len(fx.links()) == 1 and len(fx.calls) == 2  # the rejected call is still recorded


def test_fixture_validates_amount_and_currency():
    fx = FixtureRazorpayClient()
    for bad in ({"amount": 0}, {"amount": -5}, {"amount": "499"}, {"amount": True}):
        with pytest.raises(RazorpayError) as info:
            fx.create_payment_link(_payload(**bad))
        assert info.value.status == 400
    with pytest.raises(RazorpayError):
        fx.create_payment_link(_payload(currency="RUPEES"))
    with pytest.raises(RazorpayError):
        fx.create_payment_link(_payload(ref="x" * 41))
    assert fx.links() == []


def test_fixture_fetch_and_mark_paid():
    fx = FixtureRazorpayClient()
    link = fx.create_payment_link(_payload())
    assert fx.fetch_payment_link(link["id"])["status"] == "created"
    paid = fx.mark_paid(link["id"])
    assert paid["status"] == "paid" and paid["amount_paid"] == 49900
    assert fx.fetch_payment_link(link["id"])["status"] == "paid"
    partial = fx.create_payment_link(_payload(ref="ref-0003"))
    assert fx.mark_paid(partial["id"], amount_paid=100)["status"] == "partially_paid"
    with pytest.raises(RazorpayError) as info:
        fx.fetch_payment_link("plink_doesnotexist")
    assert info.value.status == 400
    with pytest.raises(RazorpayError):
        fx.mark_paid("plink_doesnotexist")


def test_fixture_lists_links_by_reference_id():
    fx = FixtureRazorpayClient()
    link = fx.create_payment_link(_payload())
    assert fx.list_payment_links(reference_id="ref-0001") == [link]
    assert fx.list_payment_links(reference_id="ref-none") == []
    assert fx.calls[-1] == ("list_payment_links", {"reference_id": "ref-none"})


def test_faulting_wrapper_passes_lookups_through():
    fx = FixtureRazorpayClient()
    link = fx.create_payment_link(_payload())
    for fault in ("razorpay_429", "razorpay_5xx"):
        fc = FaultingRazorpayClient(fx, fault)
        assert fc.list_payment_links(reference_id="ref-0001") == [link] and fc.create_calls == 0


def test_fixture_without_reference_id_still_mints_unique_ids():
    fx = FixtureRazorpayClient()
    p = _payload()
    del p["reference_id"]
    first, second = fx.create_payment_link(dict(p)), fx.create_payment_link(dict(p))
    assert first["id"] != second["id"] and first["reference_id"] is None


def test_faulting_429_twice_then_delegates():
    fx = FixtureRazorpayClient()
    fc = FaultingRazorpayClient(fx, "razorpay_429")
    assert fc.name == "fault:razorpay_429"
    for _ in range(2):
        with pytest.raises(RazorpayError) as info:
            fc.create_payment_link(_payload())
        assert info.value.status == 429
    link = fc.create_payment_link(_payload())
    assert link["id"].startswith("plink_") and fc.create_calls == 3 and len(fx.calls) == 1
    assert fc.fetch_payment_link(link["id"])["id"] == link["id"] and fc.fetch_calls == 1


def test_faulting_5xx_always_fails_create_but_passes_fetch():
    fx = FixtureRazorpayClient()
    seeded = fx.create_payment_link(_payload(ref="seeded"))
    fc = FaultingRazorpayClient(fx, "razorpay_5xx")
    for _ in range(5):
        with pytest.raises(RazorpayError) as info:
            fc.create_payment_link(_payload())
        assert info.value.status == 502 and info.value.code == "SERVER_ERROR"
    assert fc.create_calls == 5 and len(fx.links()) == 1
    assert fc.fetch_payment_link(seeded["id"])["id"] == seeded["id"]


def test_faulting_rejects_unknown_fault():
    with pytest.raises(ValueError):
        FaultingRazorpayClient(FixtureRazorpayClient(), "llm_timeout")


def test_get_client_is_fixture_singleton_without_keys():
    faults.clear()
    reset_fixture()
    a, b = get_client(), get_client()
    assert isinstance(a, FixtureRazorpayClient) and a is b and a.name == "fixture"
    a.create_payment_link(_payload())
    assert len(get_client().links()) == 1  # the fixture "server" remembers across calls
    reset_fixture()
    assert get_client() is not a and get_client().links() == []


@pytest.mark.parametrize("fault,status", [("razorpay_429", 429), ("razorpay_5xx", 502)])
def test_get_client_wraps_fixture_when_fault_active(fault, status):
    faults.clear()
    reset_fixture()
    faults.activate(fault)
    try:
        client = get_client()
        assert isinstance(client, FaultingRazorpayClient) and client.inner is rc.fixture()
        with pytest.raises(RazorpayError) as info:
            client.create_payment_link(_payload())
        assert info.value.status == status
    finally:
        faults.clear()
        reset_fixture()


def test_live_refuses_non_test_keys():
    with pytest.raises(RuntimeError):
        LiveRazorpayClient("rzp_live_abc", "secret")
    with pytest.raises(RuntimeError):
        LiveRazorpayClient("", "secret")
    with pytest.raises(RuntimeError):
        LiveRazorpayClient("rzp_test_abc", "")
    client = LiveRazorpayClient("rzp_test_abc", "secret")
    assert client.name == "live" and "secret" not in repr(client)


class _FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status, body: bytes):
    return urllib.error.HTTPError("https://api.razorpay.com/v1/payment_links", status, "err", None, io.BytesIO(body))


def test_live_sends_basic_auth_json_and_parses_response(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        return _FakeResponse(200, json.dumps({"id": "plink_live00000001", "status": "created",
                                              "short_url": "https://rzp.io/i/abcd1234"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LiveRazorpayClient("rzp_test_abc", "s3cret")
    out = client.create_payment_link(_payload())
    req = seen["req"]
    assert out["id"] == "plink_live00000001"
    assert req.full_url == "https://api.razorpay.com/v1/payment_links" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"rzp_test_abc:s3cret").decode()
    assert req.get_header("User-agent") == "payment-recovery-agent/0.1"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data.decode()) == _payload() and seen["timeout"] == 20.0

    fetched = client.fetch_payment_link("plink_live00000001")
    assert fetched["status"] == "created"
    assert seen["req"].full_url.endswith("/payment_links/plink_live00000001") and seen["req"].get_method() == "GET"


def test_live_lists_links_by_reference_id_and_filters_the_answer(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        body = {"entity": "collection", "count": 2, "payment_links": [
            {"id": "plink_A", "reference_id": "ref 1", "short_url": "https://rzp.io/i/a"},
            {"id": "plink_B", "reference_id": "other", "short_url": "https://rzp.io/i/b"}]}
        return _FakeResponse(200, json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LiveRazorpayClient("rzp_test_abc", "s3cret")
    out = client.list_payment_links(reference_id="ref 1")
    assert [x["id"] for x in out] == ["plink_A"]
    assert seen["req"].full_url == "https://api.razorpay.com/v1/payment_links?reference_id=ref+1"
    assert seen["req"].get_method() == "GET" and seen["req"].data is None
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(200, b'{"items": []}'))
    assert client.list_payment_links(reference_id="ref 1") == []


def test_live_parses_json_error_body(monkeypatch):
    body = json.dumps({"error": {"code": "BAD_REQUEST_ERROR", "description": "Too many requests",
                                 "source": "NA", "step": "NA", "reason": "NA", "metadata": {}}}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429, body)))
    client = LiveRazorpayClient("rzp_test_abc", "s3cret")
    with pytest.raises(RazorpayError) as info:
        client.create_payment_link(_payload())
    err = info.value
    assert (err.status, err.code, err.description) == (429, "BAD_REQUEST_ERROR", "Too many requests")
    assert err.details["reason"] == "NA"


def test_live_tolerates_non_json_error_body(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(_http_error(502, b"<html>Bad Gateway</html>")))
    client = LiveRazorpayClient("rzp_test_abc", "s3cret")
    with pytest.raises(RazorpayError) as info:
        client.create_payment_link(_payload())
    assert info.value.status == 502 and info.value.code == "HTTP_502" and "Bad Gateway" in info.value.description


def test_live_maps_transport_failures_to_status_zero(monkeypatch):
    for exc in (urllib.error.URLError("connection refused"), TimeoutError("timed out"), ConnectionResetError()):
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, exc=exc: (_ for _ in ()).throw(exc))
        client = LiveRazorpayClient("rzp_test_abc", "s3cret")
        with pytest.raises(RazorpayError) as info:
            client.create_payment_link(_payload())
        assert info.value.status == 0 and info.value.code == "NETWORK"


def test_live_rejects_non_json_success_body(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(200, b"not json"))
    client = LiveRazorpayClient("rzp_test_abc", "s3cret")
    with pytest.raises(RazorpayError) as info:
        client.fetch_payment_link("plink_x")
    assert info.value.code == "BAD_RESPONSE" and info.value.status == 200
