"""SQLAlchemy models and async engine setup."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# SQLite has no native timezone support and hands back naive datetimes, which
# then cannot be compared with the aware values we produce. Normalising every
# timestamp to UTC-aware on the way out keeps the arithmetic safe.
class UtcDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Proxy(Base):
    """An outbound proxy that accounts can be pinned to."""

    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    url: Mapped[str] = mapped_column(String(512))
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    password: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow)

    accounts: Mapped[list["Account"]] = relationship(back_populates="proxy")


class Account(Base):
    """A ModelScope account whose magicube balance funds video generation."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="active")
    # encrypted cookie jar; see app/crypto.py
    cookie_jar: Mapped[str] = mapped_column(Text, default="")
    auth_source: Mapped[str] = mapped_column(String(32), default="cookie")
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    proxy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("proxies.id"), nullable=True)
    # True when the proxy was picked by the pool rather than by the operator.
    proxy_auto_assigned: Mapped[bool] = mapped_column(Boolean, default=False)
    # The User-Agent of the browser that produced this session. Using a
    # different one is an identity change upstream can see.
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)

    total_balance: Mapped[float] = mapped_column(Float, default=0.0)
    available_balance: Mapped[float] = mapped_column(Float, default=0.0)
    # Magicubes reserved by jobs that are currently running upstream.
    frozen_balance: Mapped[float] = mapped_column(Float, default=0.0)
    balance_checked_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)

    running_jobs: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)
    last_validated_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)
    # Last time the daily-login grant was claimed. The grant is credited by
    # reading /api/v1/users/login/info, at most once a day, and expires the
    # next day, so this is what keeps the claim from firing on every check.
    daily_grant_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow)

    proxy: Mapped[Optional[Proxy]] = relationship(back_populates="accounts")

    @property
    def browser(self) -> str:
        """Short label for the browser that produced this session."""
        if self.user_agent and "Firefox" in self.user_agent:
            return "firefox"
        return "chrome"


class ApiKey(Base):
    """Key used by clients of our OpenAI-compatible endpoint."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)


class ModelAlias(Base):
    """Maps an OpenAI-facing model name onto a civision model."""

    __tablename__ = "model_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(120), unique=True)
    civision_value: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")


class VideoJob(Base):
    """One OpenAI-style video generation request."""

    __tablename__ = "video_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(120))
    prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    seconds: Mapped[str] = mapped_column(String(8), default="4")
    size: Mapped[str] = mapped_column(String(16), default="720x1280")
    width: Mapped[int] = mapped_column(Integer, default=720)
    height: Mapped[int] = mapped_column(Integer, default=1280)
    guidance_scale: Mapped[float] = mapped_column(Float, default=7.5)
    num_inference_steps: Mapped[int] = mapped_column(Integer, default=50)

    status: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    upstream_task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    upstream_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    local_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_reference_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The end frame of a first-to-last-frame job. Upstream has no separate
    # predict type for it: the second image is what makes the job an FLF one.
    input_reference_end_url: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )

    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    estimated_moli: Mapped[float] = mapped_column(Float, default=0.0)
    actual_moli: Mapped[float] = mapped_column(Float, default=0.0)
    queue_position: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow)
    created_at_ts: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(UtcDateTime)

    account: Mapped[Optional[Account]] = relationship()

    @property
    def created_at_unix(self) -> int:
        if self.created_at_ts:
            return self.created_at_ts
        return int(self.created_at.timestamp())


class UsageLog(Base):
    """Append-only record of every generation attempt."""

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    model: Mapped[str] = mapped_column(String(120))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    seconds: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32))
    moli_estimated: Mapped[float] = mapped_column(Float, default=0.0)
    moli_actual: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow)


class Setting(Base):
    """Small key/value store for tunables edited in the admin UI."""

    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow
    )
