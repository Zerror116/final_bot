from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class MissingDeliveryItem(AbstractModel):
    __tablename__ = "missing_delivery_items"
    __table_args__ = (
        Index("ix_missing_delivery_items_status", "status"),
        Index("ix_missing_delivery_items_post_id", "post_id"),
        Index("ix_missing_delivery_items_phone", "phone"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id = mapped_column(Integer, nullable=True)
    reservation_id = mapped_column(Integer, nullable=True)
    unit_index = mapped_column(Integer, nullable=False, default=0)
    post_id = mapped_column(Integer, nullable=False)
    user_id = mapped_column(BIGINT, nullable=False)
    user_name = mapped_column(String, nullable=True)
    phone = mapped_column(String, nullable=False)
    delivery_address = mapped_column(Text, nullable=True)
    item_description = mapped_column(Text, nullable=False)
    photo = mapped_column(Text, nullable=True)
    quantity = mapped_column(Integer, nullable=False, default=1)
    price = mapped_column(Integer, nullable=False, default=0)
    collector_user_id = mapped_column(BIGINT, nullable=True)
    collector_name = mapped_column(String, nullable=True)
    missing_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
    status = mapped_column(String, nullable=False, default="missing")
    resolved_at = mapped_column(DateTime, nullable=True)
    resolved_by_user_id = mapped_column(BIGINT, nullable=True)
