import base64
import json


def extract_jwt_payload(token: str) -> dict:
    try:
        # Handle "Bearer " prefix
        if token.startswith("Bearer "):
            token = token[7:]

        parts = token.split(".")
        if len(parts) < 2:
            return {}

        payload_b64 = parts[1]
        # Fix padding
        padding = len(payload_b64) % 4
        if padding:
            payload_b64 += "=" * (4 - padding)

        return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


token = "Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6IjE5MzQ0ZTY1LWJiYzktNDRkMS1hOWQwLWY5NTdiMDc5YmQwZSIsInR5cCI6IkpXVCJ9.eyJhdWQiOlsiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS92MSJdLCJjbGllbnRfaWQiOiJhcHBfWDh6WTZ2VzJwUTl0UjNkRTduSzFqTDVnSCIsImV4cCI6MTc3NzQ4NDIyOCwiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9hdXRoIjp7ImNoYXRncHRfYWNjb3VudF9pZCI6ImYwYWI1YjZkLTI5NWYtNGU5ZS05ZjRlLTJlZDVhODcwNDM2MyIsImNoYXRncHRfYWNjb3VudF91c2VyX2lkIjoidXNlci1NUlhUcmVZVEVWeXFBNjhUM1k2SnpBUklfX2YwYWI1YjZkLTI5NWYtNGU5ZS05ZjRlLTJlZDVhODcwNDM2MyIsImNoYXRncHRfY29tcHV0ZV9yZXNpZGVuY3kiOiJub19jb25zdHJhaW50IiwiY2hhdGdwdF9wbGFuX3R5cGUiOiJmcmVlIiwiY2hhdGdwdF91c2VyX2lkIjoidXNlci1NUlhUcmVZVEVWeXFBNjhUM1k2SnpBUkkiLCJ1c2VyX2lkIjoidXNlci1NUlhUcmVZVEVWeXFBNjhUM1k2SnpBUkkifSwiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9tZmEiOnsicmVxdWlyZWQiOiJ5ZXMifSwiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9wcm9maWxlIjp7ImVtYWlsIjoiczNudGluM2w4QGdtYWlsLmNvbSIsImVtYWlsX3ZlcmlmaWVkIjp0cnVlfSwiaWF0IjoxNzc2NjIwMjI4LCJpc3MiOiJodHRwczovL2F1dGgub3BlbmFpLmNvbSIsImp0aSI6ImIxMWUxOGE2LWIwODMtNDY3OC04MTk4LTU5N2E3MTY5MTRiZiIsIm5iZiI6MTc3NjYyMDIyOCwicHdkX2F1dGhfdGltZSI6MTc3NDM0MDU4MDYwMSwic2NwIjpbIm9wZW5pZCIsImVtYWlsIiwicHJvZmlsZSIsIm9mZmxpbmVfYWNjZXNzIiwibW9kZWwucmVxdWVzdCIsIm1vZGVsLnJlYWQiLCJvcmdhbml6YXRpb24ucmVhZCIsIm9yZ2FuaXphdGlvbi53cml0ZSJdLCJzZXNzaW9uX2lkIjoiYXV0aHNlc3NfTmJ4S3JqUlVtd2M2cGVUeFp5QWV5UEgyIiwic2wiOnRydWUsInN1YiI6Imdvb2dsZS1vYXV0aDJ8MTAzMzczMTY3NjY4NjcyMTczNDY4In0.44qjLMV7N-QIY30"

print(json.dumps(extract_jwt_payload(token), indent=2))
