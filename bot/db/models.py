from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user")


class Flow(Base):
    __tablename__ = "flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_weeks: Mapped[int] = mapped_column(Integer)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)
    sales_open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sales_close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    memberships: Mapped[list["Membership"]] = relationship(back_populates="flow")
    payments: Mapped[list["Payment"]] = relationship(back_populates="flow")

    __table_args__ = (
        UniqueConstraint("start_at", "is_free", name="uq_flows_start_is_free"),
    )


class MembershipStatus(str):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELED = "canceled"


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("flows.id"), index=True)
    status: Mapped[str] = mapped_column(
        Enum(
            MembershipStatus.ACTIVE,
            MembershipStatus.EXPIRED,
            MembershipStatus.CANCELED,
            name="membership_status",
        ),
        default=MembershipStatus.ACTIVE,
    )
    access_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    access_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grace_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pay_later_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pay_later_deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_payment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("payments.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped["User"] = relationship(back_populates="memberships")
    flow: Mapped["Flow"] = relationship(back_populates="memberships")
    last_payment: Mapped[Optional["Payment"]] = relationship(
        foreign_keys=[last_payment_id]
    )

    __table_args__ = (
        UniqueConstraint("user_id", "flow_id", name="uq_memberships_user_flow"),
    )


class PaymentStatus(str):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    NEEDS_REVIEW = "needs_review"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    flow_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("flows.id"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(
        Enum(
            PaymentStatus.PENDING,
            PaymentStatus.PAID,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
            PaymentStatus.NEEDS_REVIEW,
            name="payment_status",
        ),
        default=PaymentStatus.PENDING,
    )
    amount_rub: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="payments")
    flow: Mapped[Optional["Flow"]] = relationship(back_populates="payments")


class Promo(Base):
    __tablename__ = "promos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    flow_id: Mapped[Optional[int]] = mapped_column(ForeignKey("flows.id"), nullable=True)
    starts_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PromoCode(Base):
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    value_int: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    starts_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UserPromo(Base):
    __tablename__ = "user_promos"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True)
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
