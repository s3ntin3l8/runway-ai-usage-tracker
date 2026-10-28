"""Keep the forward-auth bypass lists aligned with the sidecar-called fleet routes."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLEET_PREFIX = "/api/v1/fleet"
# Called by a sidecar before it has any session, but not HMAC-signed: the one-time pairing
# code is the credential (rate limited by Runway). It must bypass a proxy's login too.
CODE_AUTHENTICATED_ROUTES = {f"{FLEET_PREFIX}/pair"}


def _hmac_only_fleet_routes() -> set[str]:
    source = "\n".join(
        path.read_text() for path in sorted((ROOT / "app/api/endpoints").glob("fleet*.py"))
    )
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
    assert "Runway applies its own request checks" in docs
    expected = _hmac_only_fleet_routes()
    bypass_rules = []
    for router_name, rule in re.findall(r"traefik\.http\.routers\.([^.]+)\.rule=([^\n]+)", docs):
        routes = re.findall(r"Path\(`([^`]+)`\)", rule)
        fleet_routes = {route for route in routes if route.startswith(f"{FLEET_PREFIX}/")}
        if fleet_routes:
            bypass_rules.append((router_name, rule, fleet_routes))

    documented: list[str] = []
    for router_name, rule, routes in bypass_rules:
        documented.extend(routes)
        assert all(rule.count(f"Path(`{route}`)") == 1 for route in routes)
        assert re.findall(
            rf"traefik\.http\.routers\.{re.escape(router_name)}\.service=([^\s`]+)",
            docs,
        ) == ["runway"]
    assert sorted(documented) == sorted(expected | CODE_AUTHENTICATED_ROUTES)
    assert len(documented) == len(set(documented))


def _fleet_routes() -> set[str]:
    """Every route registered with ``@router.<method>("/path")`` in the fleet modules (AST, so a
    commented-out decorator does not count)."""
    source = "\n".join(
        path.read_text() for path in sorted((ROOT / "app/api/endpoints").glob("fleet*.py"))
    )
    routes: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "router"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                routes.add(f"{FLEET_PREFIX}{decorator.args[0].value}")
    return routes


def test_pairing_route_exists_and_is_code_authenticated_not_hmac():
    # The route has to be a real registered route, and must stay out of the HMAC-only set: if it
    # ever starts calling an HMAC helper it moves there and the constant must not double count it.
    assert CODE_AUTHENTICATED_ROUTES <= _fleet_routes()
    assert not CODE_AUTHENTICATED_ROUTES & _hmac_only_fleet_routes()


def test_documented_route_table_lists_the_same_sidecar_routes():
    docs = (ROOT / "docs/forward-auth.md").read_text()
    expected = _hmac_only_fleet_routes() | CODE_AUTHENTICATED_ROUTES
    rows = re.findall(r"^\s*\| `(?:GET|POST) (/api/v1/fleet/[^`\s]+)` \|", docs, re.M)
    assert sorted(rows) == sorted(expected)
    # The prose says "five"; changing the set must change that wording too (forward-auth.md
    # section 5, and the "five routes" bullet in docs/deployment.md).
    assert len(expected) == 5
    assert "Five fleet routes are called by sidecars" in docs
    assert "five routes must bypass it" in (ROOT / "docs/deployment.md").read_text()


def test_authentik_unauthenticated_paths_list_the_same_sidecar_routes():
    docs = (ROOT / "docs/forward-auth.md").read_text()
    block = re.search(r"Unauthenticated URLs.*?```text\n(.*?)```", docs, re.S)
    assert block, "the Authentik unauthenticated-paths block is missing from forward-auth.md"
    listed = set()
    for line in block.group(1).splitlines():
        match = re.fullmatch(r"\^(/api/v1/fleet/[^$]+?)/\?\$", line.strip())
        if match:
            listed.add(match.group(1))
    assert listed == _hmac_only_fleet_routes() | CODE_AUTHENTICATED_ROUTES
