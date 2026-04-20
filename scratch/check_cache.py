import asyncio
import json
import sys

# Add app to path
sys.path.append("/home/bjoern/projects/ai-usage-tracker")

from app.services.token_cache import token_cache


async def main():
    data = await token_cache.get_accounts("chatgpt")
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
