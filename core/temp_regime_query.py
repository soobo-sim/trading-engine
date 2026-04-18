import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.shared.signals import classify_regime

async def main():
    engine = create_async_engine(
        'postgresql+asyncpg://trader:trader_password_123@trader-postgres:5432/trader_db',
        echo=False,
    )
    async with engine.begin() as conn:
        result = await conn.execute(text(
            "SELECT open_time, close, high, low FROM gmoc_candles WHERE pair = 'btc_jpy' ORDER BY open_time DESC LIMIT 100"
        ))
        rows = result.fetchall()
    await engine.dispose()
    if not rows:
        print('NO_CANDLES')
        return
    rows = rows[::-1]
    print('TOTAL', len(rows))
    for r in rows[-15:]:
        print(r[0].isoformat(), float(r[1]), float(r[2]), float(r[3]))

if __name__ == '__main__':
    asyncio.run(main())
