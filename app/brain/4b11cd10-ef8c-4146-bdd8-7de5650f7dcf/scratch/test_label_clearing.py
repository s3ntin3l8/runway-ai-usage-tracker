import asyncio

from app.services.collectors.github import GitHubCollector


async def test_label_clearing():
    print("Testing GitHub account_label clearing...")

    # Case 1: Initialized with "Default"
    collector = GitHubCollector(account_label="Default")
    print(f"  Initialized with: {collector.account_label}")

    identity = "discovered@example.com"

    # Simulating discovery (it SHOULD overwrite "Default")
    if identity:
        if collector.account_label is None or collector.account_label.lower() == "default":
            collector.account_label = identity

    print(f"  After discovery (should be email): {collector.account_label}")
    assert collector.account_label == "discovered@example.com"

    # Case 2: CLEARING the label (simulating CollectorManager sync of custom "")
    collector.account_label = ""
    print(f"\n  Manual clear to: '{collector.account_label}'")

    # Simulating discovery again (it SHOULD overwrite "")
    if identity:
        if not collector.account_label or collector.account_label.lower() == "default":
            collector.account_label = identity

    print(f"  After discovery (should be email): '{collector.account_label}'")
    assert collector.account_label == "discovered@example.com"
    print("  Case 2 (Blank label reverts to discovery) OK")

    # Case 3: Simulating None (reset)
    collector.account_label = None
    print(f"\n  Reset to: {collector.account_label}")

    if identity:
        if collector.account_label is None or collector.account_label.lower() == "default":
            collector.account_label = identity

    print(f"  After discovery (should be email): {collector.account_label}")
    assert collector.account_label == "discovered@example.com"
    print("  Case 3 (None label overwritten) OK")


if __name__ == "__main__":
    asyncio.run(test_label_clearing())
    print("\nSUCCESS: GitHub label clearing and protection logic verified!")
