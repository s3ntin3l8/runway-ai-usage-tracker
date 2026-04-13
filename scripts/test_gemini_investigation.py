"""
Investigation script for Gemini API - testing different project parameters
to find gemini-3 model quotas.

This script tests:
1. LoadCodeAssist to get cloudaicompanionProject
2. Quota API with various project parameters
3. Compare responses to find gemini-3 models
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

CLIENT_ID = os.getenv("GEMINI_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GEMINI_OAUTH_CLIENT_SECRET", "")
CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"

# ANSI colors for output
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def print_section(title):
    """Print a formatted section header."""
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}{title}{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def print_bucket_comparison(buckets, label):
    """Print quota buckets in a formatted way."""
    print(f"\n{BLUE}{label}:{RESET}")
    if not buckets:
        print(f"  {YELLOW}No buckets found{RESET}")
        return

    for b in buckets:
        model = b.get("modelId", "unknown")
        fraction = b.get("remainingFraction", 0)
        reset = b.get("resetTime", "N/A")
        token_type = b.get("tokenType", "unknown")

        # Color based on usage
        if fraction == 1.0:
            color = GREEN  # 100% remaining = 0% used
        elif fraction > 0.5:
            color = YELLOW
        else:
            color = RED

        print(f"  {color}• {model}{RESET}")
        print(f"    Token Type: {token_type}")
        print(
            f"    Remaining Fraction: {fraction} ({int(fraction * 100)}% remaining = {int((1 - fraction) * 100)}% used)"
        )
        print(f"    Reset: {reset}")


async def refresh_token(client, creds):
    """Refresh OAuth token if needed."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        print(f"{RED}ERROR: No refresh token available{RESET}")
        return None

    resp = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )

    if resp.status_code != 200:
        print(f"{RED}Token refresh failed: {resp.status_code}{RESET}")
        print(resp.text)
        return None

    new_data = resp.json()
    creds["access_token"] = new_data["access_token"]
    creds["expiry_date"] = int(time.time() * 1000) + (new_data["expires_in"] * 1000)
    print(f"{GREEN}✓ Token refreshed successfully{RESET}")
    return creds


async def test_load_code_assist(client, headers):
    """Test loadCodeAssist endpoint and extract project info."""
    print_section("1. Testing loadCodeAssist Endpoint")

    resp = await client.post(
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
        headers=headers,
    )

    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"\n{BOLD}Response Structure:{RESET}")
        print(json.dumps(data, indent=2))

        # Extract key fields
        current_tier = data.get("currentTier", {})
        paid_tier = data.get("paidTier", {})
        project = data.get("cloudaicompanionProject", "")

        print(f"\n{BOLD}Extracted Info:{RESET}")
        print(f"  Current Tier ID: {current_tier.get('id', 'N/A')}")
        print(f"  Current Tier Name: {current_tier.get('name', 'N/A')}")
        print(f"  Paid Tier ID: {paid_tier.get('id', 'N/A')}")
        print(f"  Paid Tier Name: {paid_tier.get('name', 'N/A')}")
        print(f"  {YELLOW}Project ID: {project}{RESET}")

        # Check for other project-related fields
        if "allowedTiers" in data:
            print(f"\n  Allowed Tiers ({len(data['allowedTiers'])}):")
            for tier in data["allowedTiers"]:
                print(f"    - {tier.get('id')}: {tier.get('name')}")

        return project
    print(f"{RED}Error: {resp.text}{RESET}")
    return None


async def test_quota_with_project(client, headers, project_id, label):
    """Test quota endpoint with specific project parameter."""
    body = {"project": project_id} if project_id else {}

    resp = await client.post(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        json=body,
        headers=headers,
    )

    print(f"\n{BOLD}Testing: {label}{RESET}")
    print(f"Request body: {json.dumps(body)}")
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        buckets = data.get("buckets", [])
        print_bucket_comparison(buckets, f"Found {len(buckets)} quota buckets")
        return buckets
    print(f"{RED}Error: {resp.text}{RESET}")
    return []


async def test_project_discovery(client, headers):
    """Try to discover projects via cloudresourcemanager API."""
    print_section("3. Testing Project Discovery")

    # Try listing projects
    resp = await client.get(
        "https://cloudresourcemanager.googleapis.com/v1/projects", headers=headers
    )

    print("cloudresourcemanager.googleapis.com/v1/projects")
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        projects = data.get("projects", [])
        print(f"\nFound {len(projects)} projects:")

        gemini_projects = []
        for proj in projects:
            proj_id = proj.get("projectId", "")
            name = proj.get("name", "")
            labels = proj.get("labels", {})

            # Check for gen-lang-client pattern or generative-language label
            is_gemini = "gen-lang-client" in proj_id or labels.get("generative-language")

            if is_gemini:
                gemini_projects.append(proj)
                print(f"  {GREEN}• {proj_id}{RESET} ({name})")
                if labels:
                    print(f"    Labels: {labels}")
            else:
                print(f"  • {proj_id} ({name})")

        return gemini_projects
    print(f"{RED}Error: {resp.text}{RESET}")
    return []


async def test_v2_endpoint(client, headers, project_id):
    """Try v2 endpoint if it exists."""
    print_section("4. Testing v2 Endpoint (Experimental)")

    body = {"project": project_id} if project_id else {}

    resp = await client.post(
        "https://cloudcode-pa.googleapis.com/v2:retrieveUserQuota",
        json=body,
        headers=headers,
    )

    print("Endpoint: v2:retrieveUserQuota")
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print("\nResponse:")
        print(json.dumps(data, indent=2))
        return data.get("buckets", [])
    print(f"{YELLOW}v2 endpoint not available or returned error{RESET}")
    print(resp.text[:200] if resp.text else "No response body")
    return []


async def compare_all_models():
    """Main investigation function."""
    print_section("GEMINI API INVESTIGATION")
    print(f"Credentials path: {CREDS_PATH}")
    print(f"Client ID configured: {'Yes' if CLIENT_ID else 'No'}")
    print(f"Client Secret configured: {'Yes' if CLIENT_SECRET else 'No'}")

    if not CREDS_PATH.exists():
        print(f"{RED}ERROR: Credentials not found at {CREDS_PATH}{RESET}")
        return

    if not CLIENT_ID or not CLIENT_SECRET:
        print(
            f"{RED}ERROR: GEMINI_OAUTH_CLIENT_ID and GEMINI_OAUTH_CLIENT_SECRET must be set{RESET}"
        )
        return

    with open(CREDS_PATH) as f:
        creds = json.load(f)

    async with httpx.AsyncClient() as client:
        # Check and refresh token if needed
        expiry = creds.get("expiry_date", 0)
        now = time.time() * 1000

        if expiry < now:
            print(f"{YELLOW}Token expired, refreshing...{RESET}")
            creds = await refresh_token(client, creds)
            if not creds:
                return
        else:
            print(
                f"{GREEN}Token is valid (expires in {int((expiry - now) / 1000 / 60)} minutes){RESET}"
            )

        token = creds.get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Get project info from loadCodeAssist
        discovered_project = await test_load_code_assist(client, headers)

        # 2. Test quota with different project parameters
        print_section("2. Testing Quota Endpoint with Different Project Parameters")

        # Test A: Empty project (current implementation)
        buckets_empty = await test_quota_with_project(client, headers, "", "Empty project ('')")

        # Test B: No project key at all
        print(f"\n{BOLD}Testing: No project key in body{RESET}")
        resp = await client.post(
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
            json={},
            headers=headers,
        )
        print("Request body: {}")
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            buckets_none = data.get("buckets", [])
            print_bucket_comparison(buckets_none, f"Found {len(buckets_none)} quota buckets")
        else:
            print(f"{RED}Error: {resp.text}{RESET}")
            buckets_none = []

        # Test C: Discovered project from loadCodeAssist
        buckets_discovered = []
        if discovered_project:
            buckets_discovered = await test_quota_with_project(
                client,
                headers,
                discovered_project,
                f"Discovered project ('{discovered_project}')",
            )

        # 3. Try project discovery
        gemini_projects = await test_project_discovery(client, headers)

        # Test D: Any discovered gen-lang-client projects
        buckets_other_projects = []
        if gemini_projects:
            for proj in gemini_projects[:3]:  # Test first 3
                proj_id = proj.get("projectId")
                if proj_id != discovered_project:
                    buckets = await test_quota_with_project(
                        client, headers, proj_id, f"Alternative project ('{proj_id}')"
                    )
                    buckets_other_projects.extend(buckets)

        # 4. Try v2 endpoint (experimental)
        buckets_v2 = await test_v2_endpoint(client, headers, discovered_project or "")

        # Summary
        print_section("SUMMARY")

        all_models = set()
        model_sources = {}

        for buckets, source in [
            (buckets_empty, "empty project"),
            (buckets_none, "no project key"),
            (buckets_discovered, f"project: {discovered_project}"),
            (buckets_other_projects, "alternative projects"),
            (buckets_v2, "v2 endpoint"),
        ]:
            for b in buckets:
                model = b.get("modelId")
                if model and model not in all_models:
                    all_models.add(model)
                    model_sources[model] = source

        print(f"\n{BOLD}Total unique models found: {len(all_models)}{RESET}")
        print("\nModels by source:")
        for model, source in sorted(model_sources.items()):
            has_gemini3 = "gemini-3" in model
            color = GREEN if has_gemini3 else RESET
            print(f"  {color}• {model}{RESET} (from {source})")

        gemini3_models = [m for m in all_models if "gemini-3" in m]
        if gemini3_models:
            print(f"\n{GREEN}✓ Found gemini-3 models:{RESET}")
            for m in gemini3_models:
                print(f"  • {m}")
        else:
            print(f"\n{YELLOW}⚠ No gemini-3 models found in any API response{RESET}")
            print("  This could mean:")
            print("  1. gemini-3 models are on a different API version")
            print("  2. Your account/tier doesn't have access to gemini-3 quotas yet")
            print("  3. gemini-3 quotas are tracked differently (not in retrieveUserQuota)")
            print("  4. The CLI aggregates model data from multiple sources")


if __name__ == "__main__":
    asyncio.run(compare_all_models())
