"""
DB 세션 팩토리 — 거래소 무관하게 동일 PostgreSQL 접속.

기존 CK/BF app/database.py 와 동일한 설정을 재사용.
DATABASE_URL 환경변수를 읽어 엔진을 생성한다.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

# ORM 모델용 Base — adapters/database/models.py 에서 공유
Base = declarative_base()


def create_db_engine(database_url: str):
    """
    async SQLAlchemy 엔진 생성.

    pool_size=10, max_overflow=20  스케줄러 + WS + REST 동시 처리 수용
    pool_timeout=30                풀 고갈 시 30초 후 에러 (무한 대기 방지)
    pool_recycle=1800              30분마다 재생성 (서버측 keep-alive 끊김 방지)
    pool_pre_ping=True             사용 전 연결 유효성 확인
    """
    return create_async_engine(
        database_url,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
    )


def create_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """AsyncSession 팩토리 생성."""
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
