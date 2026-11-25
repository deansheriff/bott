import json
import time

from app.security.signature import compute_signature, verify_signature

from .conftest import WEBHOOK_SECRET, message_payload, signed_headers

BODY = b'{"event":"message_created","id":1}'


def test_valid_signature():
    ts = str(int(time.time()))
    ok, reason = verify_signature("secret", ts, BODY, compute_signature("secret", ts, BODY))
    assert ok and reason == "ok"


def test_tampered_body_rejected():
    ts = str(int(time.time()))
    sig = compute_signature("secret", ts, BODY)
    ok, reason = verify_signature("secret", ts, BODY + b" ", sig)
    assert not ok and reason == "signature_mismatch"


def test_wrong_secret_rejected():
    ts = str(int(time.time()))
    assert not verify_signature("secret", ts, BODY, compute_signature("other", ts, BODY))[0]


def test_signature_covers_timestamp():
    ts = int(time.time())
    sig = compute_signature("secret", str(ts), BODY)
    ok, reason = verify_signature("secret", str(ts - 1), BODY, sig)
    assert not ok and reason == "signature_mismatch"


def test_stale_and_future_timestamps_rejected():
    now = time.time()
    old = str(int(now) - 301)
    future = str(int(now) + 301)
    assert verify_signature("secret", old, BODY, compute_signature("secret", old, BODY), now=now) == (False, "stale_timestamp")
    assert verify_signature("secret", future, BODY, compute_signature("secret", future, BODY), now=now) == (False, "stale_timestamp")
    edge = str(int(now) - 299)
    assert verify_signature("secret", edge, BODY, compute_signature("secret", edge, BODY), now=now)[0]


def test_missing_or_malformed_headers():
    ts = str(int(time.time()))
    good = compute_signature("secret", ts, BODY)
    assert verify_signature("secret", None, BODY, good)[1] == "missing_headers"
    assert verify_signature("secret", ts, BODY, None)[1] == "missing_headers"
    assert verify_signature("secret", "abc", BODY, good)[1] == "bad_timestamp"
    assert verify_signature("secret", ts, BODY, good.replace("sha256=", ""))[1] == "bad_signature_format"
    assert verify_signature("", ts, BODY, good)[1] == "secret_not_configured"
    assert verify_signature("secret", ts, BODY, "sha256=ünïcode")[1] == "signature_mismatch"


def test_endpoint_rejects_bad_signature(client, fake_cw):
    body = json.dumps(message_payload()).encode()
    headers = signed_headers(body, secret="wrong")
    r = client.post("/webhooks/chatwoot", content=body, headers=headers)
    assert r.status_code == 401
    assert fake_cw.calls == []


def test_endpoint_rejects_stale(client, fake_cw):
    body = json.dumps(message_payload()).encode()
    r = client.post("/webhooks/chatwoot", content=body, headers=signed_headers(body, ts=int(time.time()) - 600))
    assert r.status_code == 401


def test_endpoint_uses_raw_body(client, fake_cw):
    # Same JSON, different whitespace: the signature is over the exact bytes received.
    payload = message_payload()
    signed_body = json.dumps(payload).encode()
    sent_body = json.dumps(payload, indent=2).encode()
    r = client.post("/webhooks/chatwoot", content=sent_body, headers=signed_headers(signed_body))
    assert r.status_code == 401


def test_endpoint_accepts_valid(client, fake_cw):
    body = json.dumps(message_payload()).encode()
    r = client.post("/webhooks/chatwoot", content=body, headers=signed_headers(body, secret=WEBHOOK_SECRET))
    assert r.status_code == 200 and r.json() == {"ok": True}
