import asyncio


async def gather_or_cancel(*awaitables):
    """Fail a batch without leaving sibling API requests running."""
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
