from datetime import date, datetime

from sqlalchemy import BIGINT, Date, DateTime, Index, Integer, String
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


class DeliveryBroadcastCampaign(AbstractModel):
    __tablename__ = "delivery_broadcast_campaigns"
    __table_args__ = (
        Index("ix_delivery_broadcast_campaigns_date", "campaign_date"),
        Index("ix_delivery_broadcast_campaigns_status", "status"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_date = mapped_column(Date, nullable=False, unique=True)
    started_at = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    cutoff_at = mapped_column(DateTime, nullable=False)
    status = mapped_column(String, nullable=False, default="active")
    started_by_user_id = mapped_column(BIGINT, nullable=True)
    last_scan_at = mapped_column(DateTime, nullable=True)
    finished_at = mapped_column(DateTime, nullable=True)
