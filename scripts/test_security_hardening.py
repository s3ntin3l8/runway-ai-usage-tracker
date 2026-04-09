import json
import time
import hmac
import hashlib
import requests
import os
from urllib.parse import urljoin

# Configuration
API_URL = "http://localhost:8767"  # Assuming test server on 8767
SECRET_KEY = os.getenv("INGEST_API_KEY", "sidecar-default-secret")


def send_signed_request(payload):
    timestamp = str(int(time.time()))
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    signature = hmac.new(
        SECRET_KEY.encode(), timestamp.encode() + body, hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp,
    }

    resp = requests.post(urljoin(API_URL, "/api/ingest"), data=body, headers=headers)
    return resp


def test_security():
    print(f"--- Starting Security Hardening Tests on {API_URL} ---")

    # 1. Test XSS Injection & Token Redaction
    payload = {
        "provider": "malicious-sidecar",
        "metrics": [
            {
                "service": "<script>alert('XSS_SERVICE')</script>",
                "icon": "☣️",
                "remaining": "100%",
                "unit": "tokens",
                "reset": "Never",
                "health": "critical",
                "pace": "<b>Malicious Pace</b>",
                "detail": "api_key:secret-token-123 <img src=x onerror=alert('XSS_DETAIL')>",
                "data_source": "api",
            }
        ],
    }

    print("\n[1] Pushing malicious payload...")
    resp = send_signed_request(payload)
    if resp.status_code != 200:
        print(f"FAILED: Ingest returned {resp.status_code}")
        print(resp.text)
        return
    print("SUCCESS: Ingested malicious payload")

    # 2. Fetch limits and inspect
    print("\n[2] Verifying redaction and escaping via /api/limits...")
    resp = requests.get(urljoin(API_URL, "/api/limits"))
    if resp.status_code != 200:
        print(f"FAILED: Fetch limits returned {resp.status_code}")
        return

    data = resp.json()
    found = False
    for card in data.get("limits", []):
        if "XSS_SERVICE" in card["service"]:
            found = True
            print(f"Found Card: {card['service']}")

            # Check Token Redaction (Backend)
            if "secret-token-123" in card["detail"]:
                print(
                    "❌ CRITICAL FAILURE: Raw token 'secret-token-123' found in detail field!"
                )
            else:
                print("✅ SUCCESS: Token 'secret-token-123' was redacted.")
                print(f"   Detail: {card['detail']}")

            # Check XSS Escaping (Frontend - will be checked in browser subagent)
            # The API response itself won't be escaped (that happens in JS),
            # but we can verify it's still there for the JS to escape.
            print(f"   API Response Detail: {card['detail']}")

    if not found:
        print("FAILED: Malicious card not found in limits!")


if __name__ == "__main__":
    test_security()
