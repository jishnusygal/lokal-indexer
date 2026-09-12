"""Persistent SQLite storage. Paths are relative to the application working directory."""
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path('data')
TORRENT_DIR = Path('torrents')
DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(f'sqlite:///{DATA_DIR / "indexer.db"}', connect_args={'check_same_thread': False, 'timeout': 30})

@event.listens_for(engine, 'connect')
def configure_sqlite(connection, _):
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=30000')

class Base(DeclarativeBase):
    pass

SessionLocal = sessionmaker(engine, expire_on_commit=False)
