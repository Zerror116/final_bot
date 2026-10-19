from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, Date, DateTime, Index, Integer, String
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class DeliveryBroadcastCampaign(AbstractModel):
    __tablename__ = "delivery_broadcast_campaigns"
    __table_args__ = (
        Index("ix_delivery_broadcast_campaigns_date", "campaign_date"),
        Index("ix_delivery_broadcast_campaigns_status", "status"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_date = mapped_column(Date, nullable=False, unique=True)
    started_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
    cutoff_at = mapped_column(DateTime, nullable=False)
    status = mapped_column(String, nullable=False, default="active")
    started_by_user_id = mapped_column(BIGINT, nullable=True)
    summary_chat_id = mapped_column(BIGINT, nullable=True)
    summary_message_id = mapped_column(BIGINT, nullable=True)
    last_scan_at = mapped_column(DateTime, nullable=True)
    finished_at = mapped_column(DateTime, nullable=True)
