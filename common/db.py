"""Shared database engine/session for all services.

Uses SQLAlchemy Core for simple, explicit, auditable SQL — deliberately
avoiding a heavy ORM for the ledger path, since every write to money
tables should be an explicit, reviewable statement.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://fintech:fintech@postgres:5432/fintech",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
