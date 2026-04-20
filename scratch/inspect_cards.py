import asyncio

from app.services.collector_manager import manager


async def inspect_cards():
    print("Inspecting all collected cards...")
    await manager.sync_manual_configs()
    cards = await manager.collect_all()
    print(f"Total cards collected: {len(cards)}")
    for i, card in enumerate(cards):
        print(f"Card {i + 1}: {card.service} - {card.remaining} (Source: {card.data_source})")


if __name__ == "__main__":
    asyncio.run(inspect_cards())
