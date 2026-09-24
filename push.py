"""Phone push notifications (Web Push / RFC 8030) for flying-session time
alerts - 30 minutes left, time's up, and running late past a scheduled
block. Deliberately dependency-free (stdlib only, like weather.py/adsb.py/
notify.py) since the Pi only has `flask` installed - so this implements
VAPID (RFC 8292) from scratch: a small pure-Python P-256 ECDSA signer for
the VAPID JWT, and a plain urllib POST to the browser's push service.

Each push now carries its alert (title/body/link) as an encrypted payload
(RFC 8291 aes128gcm, with a small pure-Python AES-GCM below), so the phone
can show it without logging in. Every alert is also still written to
push_pending, and the service worker picks those up from
/flight/push/pending when it can (an older subscription that came back
without keys, or anything else queued). See static/push-sw.js for the
client half.

Email/text delivery already exists (notify.py, SMTP + Twilio) for daily
digest-style reminders; this module is the phone-push channel for the
real-time, minute-to-minute alerts that a daily cron can't do. A given
alert category (e.g. "session about to end") can grow email/text delivery
later by calling notify.notify_user() alongside queue_and_push() below -
nothing here is push-only by design, it's just the first channel built.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# P-256 (secp256r1) field/curve arithmetic - just enough for ECDSA (ES256).
# All the constants below are the standard NIST P-256 parameters.
# ---------------------------------------------------------------------------

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (_GX, _GY)


def _inverse_mod(k, m):
    return pow(k, m - 2, m)


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1 + _A) * _inverse_mod(2 * y1, _P) % _P
    else:
        lam = (y2 - y1) * _inverse_mod(x2 - x1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _scalar_mult(k, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _random_scalar():
    """A uniform random integer in [1, N-1], via rejection sampling on
    32 CSPRNG bytes (os.urandom) - the standard, simplest-correct way to
    avoid modulo bias when generating an EC private key or an ECDSA
    per-signature nonce."""
    while True:
        k = int.from_bytes(os.urandom(32), "big")
        if 1 <= k < _N:
            return k


def generate_p256_keypair():
    """Returns (private_int, (pub_x, pub_y))."""
    d = _random_scalar()
    q = _scalar_mult(d, _G)
    return d, q


def _b64url(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")


def _b64url_decode(s):
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def public_key_b64(pub_point):
    """Uncompressed SEC1 point (0x04 || X || Y), base64url - this is the
    'applicationServerKey' shape the browser's Push API expects."""
    x, y = pub_point
    raw = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    return _b64url(raw)


def private_key_b64(priv_int):
    return _b64url(priv_int.to_bytes(32, "big"))


def private_key_from_b64(s):
    return int.from_bytes(_b64url_decode(s), "big")


def sign_es256(priv_int, message_bytes):
    """ECDSA-P256-SHA256 signature over message_bytes, returned as the raw
    r||s (64 bytes) that JWS ES256 uses (not DER)."""
    z = int.from_bytes(hashlib.sha256(message_bytes).digest(), "big")
    while True:
        k = _random_scalar()
        point = _scalar_mult(k, _G)
        if point is None:
            continue
        r = point[0] % _N
        if r == 0:
            continue
        s = (_inverse_mod(k, _N) * (z + r * priv_int)) % _N
        if s == 0:
            continue
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def build_vapid_jwt(audience, subject, priv_int, ttl_seconds=3600):
    # 1 hour: comfortably inside every push service's limit (Apple is the
    # strictest about long-lived tokens).
    header = _b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({
        "aud": audience,
        "exp": int(time.time()) + ttl_seconds,
        "sub": subject,
    }, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = _b64url(sign_es256(priv_int, signing_input))
    return f"{header}.{payload}.{sig}"


# ---------------------------------------------------------------------------
# Encrypted payloads (RFC 8291, "aes128gcm") - so the alert's title/body
# travel inside the push itself. Without this the service worker had to
# fetch /flight/push/pending with the login cookie, and on an iPhone
# home-screen app that cookie is usually gone by the time a push arrives
# (unless "Remember me" was ticked), so the alert silently never showed.
# Pure-Python AES-128 + GCM below keeps this module stdlib-only; payloads
# are a few hundred bytes, so speed doesn't matter.
# ---------------------------------------------------------------------------

_SBOX = [0] * 256


def _build_sbox():
    # Standard AES S-box, generated from GF(2^8) inverses + the affine map.
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF) ^ ((q << 3 | q >> 5) & 0xFF) ^ ((q << 4 | q >> 4) & 0xFF)
        _SBOX[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    _SBOX[0] = 0x63


_build_sbox()


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _aes128_expand_key(key):
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([a ^ b for a, b in zip(words[i - 4], t)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]


def _aes128_encrypt_block(round_keys, block):
    s = [b ^ k for b, k in zip(block, round_keys[0])]
    for rnd in range(1, 11):
        s = [_SBOX[b] for b in s]
        # ShiftRows (state is column-major: index = col*4 + row)
        s = [s[((c + r) % 4) * 4 + r] for c in range(4) for r in range(4)]
        if rnd != 10:
            out = []
            for c in range(4):
                a0, a1, a2, a3 = s[c * 4:c * 4 + 4]
                t = a0 ^ a1 ^ a2 ^ a3
                out += [a0 ^ t ^ _xtime(a0 ^ a1), a1 ^ t ^ _xtime(a1 ^ a2),
                        a2 ^ t ^ _xtime(a2 ^ a3), a3 ^ t ^ _xtime(a3 ^ a0)]
            s = out
        s = [b ^ k for b, k in zip(s, round_keys[rnd])]
    return bytes(s)


def _gf128_mul(x, y):
    r = 0xE1000000000000000000000000000000
    z = 0
    v = y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        v = (v >> 1) ^ r if v & 1 else v >> 1
    return z


def aes128_gcm_encrypt(key, nonce, plaintext):
    """AES-128-GCM with a 12-byte nonce and no associated data. Returns
    ciphertext || 16-byte tag (the layout Web Push expects)."""
    rk = _aes128_expand_key(key)
    h = int.from_bytes(_aes128_encrypt_block(rk, bytes(16)), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    ct = bytearray()
    counter = 2
    for off in range(0, len(plaintext), 16):
        ks = _aes128_encrypt_block(rk, nonce + counter.to_bytes(4, "big"))
        chunk = plaintext[off:off + 16]
        ct += bytes(a ^ b for a, b in zip(chunk, ks))
        counter += 1
    g = 0
    padded = bytes(ct) + bytes(-len(ct) % 16)
    for off in range(0, len(padded), 16):
        g = _gf128_mul(g ^ int.from_bytes(padded[off:off + 16], "big"), h)
    g = _gf128_mul(g ^ (len(ct) * 8), h)  # len(A)=0 in the high 64 bits
    tag = (g ^ int.from_bytes(_aes128_encrypt_block(rk, j0), "big")).to_bytes(16, "big")
    return bytes(ct) + tag


def _hmac_sha256(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt_payload(p256dh_b64, auth_b64, plaintext):
    """Encrypts plaintext (bytes) for one browser subscription per RFC 8291
    and returns the aes128gcm request body."""
    ua_public = _b64url_decode(p256dh_b64)
    auth_secret = _b64url_decode(auth_b64)
    ua_point = (int.from_bytes(ua_public[1:33], "big"), int.from_bytes(ua_public[33:65], "big"))
    as_private, as_point = generate_p256_keypair()
    as_public = b"\x04" + as_point[0].to_bytes(32, "big") + as_point[1].to_bytes(32, "big")
    shared = _scalar_mult(as_private, ua_point)
    ecdh_secret = shared[0].to_bytes(32, "big")
    prk_key = _hmac_sha256(auth_secret, ecdh_secret)
    ikm = _hmac_sha256(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    salt = os.urandom(16)
    prk = _hmac_sha256(salt, ikm)
    cek = _hmac_sha256(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac_sha256(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = aes128_gcm_encrypt(cek, nonce, plaintext + b"\x02")
    record_size = 4096
    return salt + record_size.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + ciphertext


# ---------------------------------------------------------------------------
# VAPID application keypair - generated once, stored in app_settings so it
# stays stable across restarts (a subscription is tied to the public key
# that was current when the browser subscribed).
# ---------------------------------------------------------------------------

# Apple's push service rejects a VAPID "sub" it doesn't consider a real
# contact (the old "mailto:admin@flywithkate.local" was one) - use the
# school's real site.
VAPID_SUBJECT = "https://flywithkate.com"


def get_or_create_vapid_keys(conn):
    """Returns (private_key_b64, public_key_b64), generating and persisting
    a new VAPID keypair the first time this is called."""
    rows = conn.execute(
        "SELECT key, value FROM app_settings WHERE key IN ('vapid_private_key', 'vapid_public_key')"
    ).fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    priv_b64 = settings.get("vapid_private_key")
    pub_b64 = settings.get("vapid_public_key")
    if priv_b64 and pub_b64:
        return priv_b64, pub_b64
    priv_int, pub_point = generate_p256_keypair()
    priv_b64 = private_key_b64(priv_int)
    pub_b64 = public_key_b64(pub_point)
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('vapid_private_key', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (priv_b64,))
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('vapid_public_key', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (pub_b64,))
    conn.commit()
    return priv_b64, pub_b64


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _sub_field(subscription, name):
    try:
        return subscription[name]
    except (KeyError, IndexError, TypeError):
        return None


def send_web_push(subscription, priv_b64, pub_b64, ttl=600, payload=None):
    """Sends one push to one subscription. subscription is a dict/Row with
    'endpoint' (plus 'p256dh'/'auth', needed to encrypt a payload - without
    them the push goes out empty and the service worker falls back to
    /flight/push/pending). payload is a dict (sent as JSON) or bytes.
    Returns (ok, status_or_none, error_or_none). A 404/410
    means the subscription is gone - the caller should delete that row so
    it isn't retried forever."""
    endpoint = subscription["endpoint"]
    parsed = urllib.parse.urlsplit(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"
    priv_int = private_key_from_b64(priv_b64)
    jwt = build_vapid_jwt(audience, VAPID_SUBJECT, priv_int)
    body = b""
    if payload is not None and _sub_field(subscription, "p256dh") and _sub_field(subscription, "auth"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        body = encrypt_payload(_sub_field(subscription, "p256dh"), _sub_field(subscription, "auth"), raw)
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("TTL", str(ttl))
    req.add_header("Urgency", "high")
    req.add_header("Authorization", f"vapid t={jwt}, k={pub_b64}")
    req.add_header("Content-Length", str(len(body)))
    if body:
        req.add_header("Content-Encoding", "aes128gcm")
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, resp.status, None
    except urllib.error.HTTPError as e:
        return False, e.code, e.reason
    except Exception as e:
        return False, None, str(e)


# ---------------------------------------------------------------------------
# Queue + deliver: called by the periodic session-alert check (flight.py),
# and reusable for any other real-time alert added later.
# ---------------------------------------------------------------------------

def queue_and_push(conn, user_id, title, body=None, tag=None, url=None):
    """Records the alert in push_pending (what the service worker's fetch
    reads) and fires a push to every device that user has subscribed on,
    pruning subscriptions the push service reports as gone. Returns a list
    of (ok, status, error) - one per device - so callers can report what
    happened (empty if the user has no devices turned on)."""
    conn.execute(
        "INSERT INTO push_pending (user_id, title, body, tag, url) VALUES (?, ?, ?, ?, ?)",
        (user_id, title, body, tag, url),
    )
    conn.commit()
    subs = conn.execute("SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchall()
    results = []  # one (ok, status, error) per device - the admin push test reports these
    if not subs:
        return results
    priv_b64, pub_b64 = get_or_create_vapid_keys(conn)
    payload = {"title": title, "body": body or "", "tag": tag or "", "url": url or ""}
    for sub in subs:
        try:
            ok, status, err = send_web_push(sub, priv_b64, pub_b64, payload=payload)
        except Exception as e:  # a bad stored key shouldn't stop the other devices
            ok, status, err = False, None, f"encrypt failed: {e}"
        results.append((ok, status, err))
        if not ok and status in (404, 410):
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
            conn.commit()
    return results
