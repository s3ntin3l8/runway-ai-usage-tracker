import hmac
import hashlib
import time
import requests
import json
import sys

# Configuration
API_URL = "http://localhost:8767/api/ingest"
API_KEY = "test-secret-key"  # Must match INGEST_API_KEY in .env or default


def send_ingest(provider, metrics, api_key, timestamp_offset=0, custom_sig=None):
    url = API_URL
    payload = {"provider": provider, "metrics": metrics}

    timestamp = str(int(time.time() + timestamp_offset))
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    if custom_sig:
        signature = custom_sig
    else:
        signature = hmac.new(
            api_key.encode(), timestamp.encode() + body, hashlib.sha256
        ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp,
    }

    print(f"\nTesting: {provider}")
    print(f"Timestamp: {timestamp}")
    print(f"Signature: {signature[:10]}...")

    try:
        resp = requests.post(url, data=body, headers=headers, timeout=5)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        return resp.status_code
    except Exception as e:
        print(f"Error: {e}")
        return None


def main():
    # 1. Valid request
    metrics = [
        {
            "service_name": "Test Service",
            "icon": "🧪",
            "remaining": "100%",
            "unit": "capacity",
            "reset": "1h",
            "health": "good",
            "pace": "Stable",
            "detail": "test metric [Test]",
        }
    ]

    print("--- Test 1: Valid HMAC ---")
    send_ingest("test-provider", metrics, API_KEY)

    # 2. Invalid Key
    print("\n--- Test 2: Invalid Secret Key ---")
    send_ingest("test-provider", metrics, "wrong-key")

    # 3. Expired Timestamp
    print("\n--- Test 3: Expired Timestamp (10 mins ago) ---")
    send_ingest("test-provider", metrics, API_KEY, timestamp_offset=-600)

    # 4. Missing Headers
    print("\n--- Test 4: Missing Headers ---")
    try:
        resp = requests.post(API_URL, json={"provider": "test", "metrics": []})
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
