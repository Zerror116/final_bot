from datetime import datetime

from sqlalchemy import BIGINT, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


class DeliveryBroadcastRecipient(AbstractModel):
    __tablename__ = "delivery_broadcast_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "phone", name="uq_delivery_broadcast_recipients_campaign_phone"),
        Index("ix_delivery_broadcast_recipients_campaign", "campaign_id"),
        Index("ix_delivery_broadcast_recipients_status", "status"),
        Index("ix_delivery_broadcast_recipients_phone", "phone"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id = mapped_column(Integer, nullable=False)
    phone = mapped_column(String, nullable=False)
    user_id = mapped_column(BIGINT, nullable=False)
    name = mapped_column(String, nullable=True)
    total_sum = mapped_column(Integer, nullable=False, default=0)
    status = mapped_column(String, nullable=False, default="sending")
    attempted_at = mapped_column(DateTime, nullable=True, default=datetime.utcnow)
    sent_at = mapped_column(DateTime, nullable=True)
    telegram_message_id = mapped_column(BIGINT, nullable=True)
    error = mapped_column(Text, nullable=True)
