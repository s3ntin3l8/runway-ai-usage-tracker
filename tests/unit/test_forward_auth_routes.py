"""Keep the forward-auth bypass list aligned with HMAC-only fleet routes."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLEET_PREFIX = "/api/v1/fleet"


def _hmac_only_fleet_routes() -> set[str]:
    source = (ROOT / "app/api/endpoints/fleet.py").read_text()
    module = ast.parse(source)
    routes: set[str] = set()
    auth_helpers = {"validate_ingest_auth", "verify_config_signature"}
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls_hmac = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in auth_helpers
            for call in ast.walk(node)
        )
        if not calls_hmac:
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if (
                not isinstance(decorator.func.value, ast.Name)
                or decorator.func.value.id != "router"
            ):
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                route = decorator.args[0].value
                if isinstance(route, str) and route.startswith("/"):
                    routes.add(f"{FLEET_PREFIX}{route}")
    return routes


def test_forward_auth_documentation_lists_every_hmac_only_route():
    docs = (ROOT / "docs/forward-auth.md").read_text()
    expected = _hmac_only_fleet_routes()
    bypass_rules = []
    for router_name, rule in re.findall(r"traefik\.http\.routers\.([^.]+)\.rule=([^\n]+)", docs):
        routes = re.findall(r"Path\(`([^`]+)`\)", rule)
        fleet_routes = {route for route in routes if route.startswith(f"{FLEET_PREFIX}/")}
        if fleet_routes:
            bypass_rules.append((router_name, rule, fleet_routes))

    assert len(bypass_rules) == 1
    router_name, rule, documented = bypass_rules[0]
    assert documented == expected
    assert all(rule.count(f"Path(`{route}`)") == 1 for route in expected)
    assert re.findall(
        rf"traefik\.http\.routers\.{re.escape(router_name)}\.service=([^\s`]+)",
        docs,
    ) == ["runway"]
