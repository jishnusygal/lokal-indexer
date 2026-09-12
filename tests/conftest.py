import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
import config
import main
import scraper
from database import Base

@pytest.fixture
def service(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "test.db"}', connect_args={'check_same_thread': False})
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (main, config, scraper):
        monkeypatch.setattr(module, 'SessionLocal', factory)
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'TORRENT_DIR', tmp_path / 'torrents')
    monkeypatch.setattr(scraper, 'TORRENT_DIR', tmp_path / 'torrents')
    with TestClient(main.app) as client:
        yield client, factory, tmp_path
    engine.dispose()
