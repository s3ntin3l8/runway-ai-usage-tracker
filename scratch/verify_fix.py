import asyncio

from app.services.token_cache import token_cache


async def test():
    await token_cache.store("test_provider", {"t": "v"}, source="test_source")
    res = await token_cache.get_with_metadata("test_provider")
    print(f"Result: {res}")
    assert res[1]["source"] == "test_source"
    print("Verification SUCCESS")


if __name__ == "__main__":
    asyncio.run(test())
