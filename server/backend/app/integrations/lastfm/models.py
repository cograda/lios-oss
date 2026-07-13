"""SQLAlchemy models for cached Last.fm scrobbles and artist metadata."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, Boolean, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class Scrobble(UserOwnedMixin, SourcedRecordMixin, Base):
    """Cached Last.fm scrobble.

    Sam inactive on Last.fm but schema is multi-user-ready — when she
    enables it later, her scrobbles land in the same table without migration.
    artist_tags stays global metadata (community tags are not per-user).
    """

    __tablename__ = "scrobbles"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "track_name", "artist_name", "played_at",
            name="uq_scrobble",
        ),
        Index("ix_scrobbles_user_played_at", "user_id", "played_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    track_name: Mapped[str] = mapped_column(String(500))
    artist_name: Mapped[str] = mapped_column(String(500))
    album_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    album_art_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mbid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    loved: Mapped[bool] = mapped_column(Boolean, default=False)
    # synced_at inherited from SourcedRecordMixin

    def __repr__(self) -> str:
        return f"<Scrobble {self.artist_name} - {self.track_name}>"


class ArtistTag(Base):
    """Genre/style tags for an artist, sourced from Last.fm's community tags."""

    __tablename__ = "artist_tags"
    __table_args__ = (
        UniqueConstraint("artist_name_lower", "tag", name="uq_artist_tag"),
        Index("ix_artist_tags_artist", "artist_name_lower"),
        Index("ix_artist_tags_tag", "tag"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_name: Mapped[str] = mapped_column(String(500))
    artist_name_lower: Mapped[str] = mapped_column(String(500))
    tag: Mapped[str] = mapped_column(String(200))
    weight: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ArtistTag {self.artist_name}: {self.tag} ({self.weight})>"
