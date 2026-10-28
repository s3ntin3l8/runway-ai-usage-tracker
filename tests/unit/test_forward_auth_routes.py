"""Keep the forward-auth bypass lists aligned with the sidecar-called fleet routes."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLEET_PREFIX = "/api/v1/fleet"
# Called by a sidecar before it has any session, but not HMAC-signed: the one-time pairing
# code is the credential (rate limited by Runway). It must bypass a proxy's login too.
CODE_AUTHENTICATED_ROUTES = {f"{FLEET_PREFIX}/pair"}


AUTH_HELPERS = {"validate_ingest_auth", "verify_config_signature"}


def _fleet_route_handlers() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every ``@router.<method>("/path")`` handler in the fleet modules, as (full route, node).

    Read through the AST, so a commented-out decorator does not count. One walk feeds every
    check below, so they cannot disagree about what a route is."""
    source = "\n".join(
        path.read_text() for path in sorted((ROOT / "app/api/endpoints").glob("fleet*.py"))
    )
    handlers: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
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
                and decorator.args[0].value.startswith("/")
            ):
                handlers.append((f"{FLEET_PREFIX}{decorator.args[0].value}", node))
    return handlers


def _fleet_routes() -> set[str]:
    return {route for route, _ in _fleet_route_handlers()}


def _hmac_only_fleet_routes() -> set[str]:
    """Routes whose handler authenticates the request itself with a sidecar HMAC."""
    return {
        route
        for route, node in _fleet_route_handlers()
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in AUTH_HELPERS
            for call in ast.walk(node)
        )
    }


_NUMBER_WORDS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
]


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
    # The prose names the count in words; changing the set must change that wording too, but a
    # harmless rewording must not break the build. Match the number word the set implies.
    word = _NUMBER_WORDS[len(expected)]
    # Anchored to the sentence that states the count, so another "five routes" elsewhere in the
    # file cannot keep the assertion green while the lead sentence goes stale.
    assert re.search(rf"\*\*Sidecar requests\.\*\*\s+{word}\b", docs, re.I), (
        f"forward-auth.md's 'Sidecar requests' bullet should open with '{word}'"
    )
    assert re.search(
        rf"\*\*Behind SSO, {word}\b", (ROOT / "docs/deployment.md").read_text(), re.I
    ), f"deployment.md's 'Behind SSO' bullet should say '{word}'"


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


def test_deployment_intro_counts_the_bullets_under_it():
    """ "Four things to know:" went stale twice while a bullet was added above it. Assert the number
    word in a "<N> things to know:" intro equals the number of bullets that follow."""
    lines = (ROOT / "docs/deployment.md").read_text().splitlines()
    intros = [
        (index, match.group(1).lower())
        for index, line in enumerate(lines)
        if (match := re.fullmatch(r"(\w+) things to know:", line.strip(), re.I))
    ]
    assert intros, "docs/deployment.md should keep its '<N> things to know:' intro"
    for index, word in intros:
        bullets = 0
        for line in lines[index + 1 :]:
            if line.startswith("- "):
                bullets += 1
            elif not line.strip() or line.startswith((" ", "\t")):
                continue  # blank lines, wrapped continuations and sub-bullets stay in the list
            elif bullets:
                break  # the first unindented line that is not a bullet ends the list
        assert word in _NUMBER_WORDS, f"unknown number word {word!r}"
        assert _NUMBER_WORDS.index(word) == bullets, (
            f"deployment.md says '{word} things to know' but lists {bullets} bullets"
        )
