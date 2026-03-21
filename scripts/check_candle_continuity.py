"""
캔들 데이터 연속성 검사 스크립트.

CK/BF/GMO 캔들 테이블에서 90일분 4H 캔들의 갭(누락)을 찾아 보고한다.

사용:
    DATABASE_URL=... python scripts/check_candle_continuity.py
    DATABASE_URL=... python scripts/check_candle_continuity.py --days 180 --timeframe 1h
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


TIMEFRAME_HOURS = {
    "1h": 1,
    "4h": 4,
}


async def check_continuity(
    db_url: str,
    days: int = 90,
    timeframe: str = "4h",
) -> dict:
    """캔들 연속성 검사. 결과를 dict로 반환."""
    engine = create_async_engine(db_url, echo=False)

    tables = [
        ("ck_candles", "pair"),
        ("bf_candles", "product_code"),
        ("gmo_candles", "pair"),
    ]
    interval_hours = TIMEFRAME_HOURS.get(timeframe, 4)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    results = {}

    async with sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        for table_name, pair_col in tables:
            # 테이블 존재 확인
            check_sql = text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_name = :tbl"
                ")"
            )
            exists_result = await session.execute(check_sql, {"tbl": table_name})
            if not exists_result.scalar():
                results[table_name] = {"status": "table_not_found"}
                continue

            # 페어 목록 조회
            pair_sql = text(
                f"SELECT DISTINCT {pair_col} FROM {table_name} "
                f"WHERE timeframe = :tf AND open_time >= :since"
            )
            pair_result = await session.execute(
                pair_sql, {"tf": timeframe, "since": since}
            )
            pairs = [row[0] for row in pair_result.fetchall()]

            if not pairs:
                results[table_name] = {"status": "no_data", "pairs": []}
                continue

            table_report = {"status": "checked", "pairs": {}}

            for pair_value in pairs:
                # 완성 캔들 조회 (시간순)
                candle_sql = text(
                    f"SELECT open_time FROM {table_name} "
                    f"WHERE {pair_col} = :pair AND timeframe = :tf "
                    f"AND open_time >= :since AND is_complete = true "
                    f"ORDER BY open_time"
                )
                candle_result = await session.execute(
                    candle_sql,
                    {"pair": pair_value, "tf": timeframe, "since": since},
                )
                times = [row[0] for row in candle_result.fetchall()]

                if not times:
                    table_report["pairs"][pair_value] = {
                        "total_candles": 0,
                        "expected_candles": 0,
                        "gaps": [],
                        "coverage_pct": 0.0,
                    }
                    continue

                # 갭 검출
                gaps = []
                expected_interval = timedelta(hours=interval_hours)
                for i in range(1, len(times)):
                    prev_t = times[i - 1]
                    curr_t = times[i]
                    # timezone-aware 비교
                    if prev_t.tzinfo is None:
                        prev_t = prev_t.replace(tzinfo=timezone.utc)
                    if curr_t.tzinfo is None:
                        curr_t = curr_t.replace(tzinfo=timezone.utc)

                    diff = curr_t - prev_t
                    if diff > expected_interval * 1.5:  # 1.5배 이상이면 갭
                        missing_count = int(diff / expected_interval) - 1
                        gaps.append({
                            "from": prev_t.isoformat(),
                            "to": curr_t.isoformat(),
                            "missing_candles": missing_count,
                        })

                # 기대 캔들 수 계산
                first_t = times[0] if times[0].tzinfo else times[0].replace(tzinfo=timezone.utc)
                last_t = times[-1] if times[-1].tzinfo else times[-1].replace(tzinfo=timezone.utc)
                total_hours = (last_t - first_t).total_seconds() / 3600
                expected = int(total_hours / interval_hours) + 1
                coverage = round(len(times) / expected * 100, 1) if expected > 0 else 0.0

                table_report["pairs"][pair_value] = {
                    "total_candles": len(times),
                    "expected_candles": expected,
                    "first": first_t.isoformat(),
                    "last": last_t.isoformat(),
                    "days_covered": round(total_hours / 24, 1),
                    "gaps": gaps,
                    "gap_count": len(gaps),
                    "total_missing": sum(g["missing_candles"] for g in gaps),
                    "coverage_pct": coverage,
                }

            results[table_name] = table_report

    await engine.dispose()
    return results


def print_report(results: dict) -> None:
    """결과를 읽기 쉽게 출력."""
    print("=" * 60)
    print("  캔들 데이터 연속성 검사 결과")
    print("=" * 60)

    for table_name, report in results.items():
        print(f"\n📊 {table_name}")
        if report.get("status") == "table_not_found":
            print("   ⚠️  테이블 없음")
            continue
        if report.get("status") == "no_data":
            print("   ⚠️  데이터 없음")
            continue

        for pair, stats in report.get("pairs", {}).items():
            total = stats["total_candles"]
            expected = stats["expected_candles"]
            coverage = stats["coverage_pct"]
            gap_count = stats.get("gap_count", 0)
            total_missing = stats.get("total_missing", 0)
            days = stats.get("days_covered", 0)

            status_icon = "✅" if coverage >= 99.0 else ("⚠️" if coverage >= 95.0 else "❌")
            print(f"   {status_icon} {pair}: {total}/{expected} 캔들 ({coverage}%) — {days}일분")

            if gap_count > 0:
                print(f"      갭 {gap_count}개 (누락 합계 {total_missing}개)")
                for g in stats["gaps"][:5]:  # 상위 5개만 출력
                    print(f"        {g['from']} → {g['to']} ({g['missing_candles']}개 누락)")
                if gap_count > 5:
                    print(f"        ... 외 {gap_count - 5}개")

    print("\n" + "=" * 60)


async def main():
    parser = argparse.ArgumentParser(description="캔들 데이터 연속성 검사")
    parser.add_argument("--days", type=int, default=90, help="검사 기간 (일)")
    parser.add_argument("--timeframe", type=str, default="4h", help="타임프레임: 1h | 4h")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("❌ DATABASE_URL 환경변수 필요")
        sys.exit(1)

    results = await check_continuity(db_url, days=args.days, timeframe=args.timeframe)
    print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
