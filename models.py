from datetime import datetime, timezone
from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from database import Base

def utcnow():
    return datetime.now(timezone.utc)

class Setting(Base):
    __tablename__ = 'settings'
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)

class Release(Base):
    __tablename__ = 'releases'
    __table_args__ = (CheckConstraint("category IN ('movie', 'tv')"), CheckConstraint('size >= 0'))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, index=True)
    category: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(BigInteger)
    pub_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    magnet_uri: Mapped[str | None] = mapped_column(Text)
    torrent_file_path: Mapped[str | None] = mapped_column(String)
    imdb_id: Mapped[str | None] = mapped_column(String, index=True)
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)

class ScraperLog(Base):
    __tablename__ = 'scraper_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String)
    items_added: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
