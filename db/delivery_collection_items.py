from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class DeliveryCollectionItem(AbstractModel):
    __tablename__ = "delivery_collection_items"
    __table_args__ = (
        UniqueConstraint("reservation_id", "unit_index", name="uq_delivery_collection_items_reservation_unit"),
        Index("ix_delivery_collection_items_delivery_id", "delivery_id"),
        Index("ix_delivery_collection_items_placed_at", "placed_at"),
        Index("ix_delivery_collection_items_post_id", "post_id"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id = mapped_column(Integer, nullable=False)
    reservation_id = mapped_column(Integer, nullable=False)
    unit_index = mapped_column(Integer, nullable=False, default=0)
    post_id = mapped_column(Integer, nullable=False)
    user_id = mapped_column(BIGINT, nullable=False)
    user_name = mapped_column(String, nullable=True)
    phone = mapped_column(String, nullable=False)
    item_description = mapped_column(Text, nullable=False)
    photo = mapped_column(Text, nullable=True)
    quantity = mapped_column(Integer, nullable=False, default=1)
    amount = mapped_column(Integer, nullable=False, default=0)
    delivery_address = mapped_column(Text, nullable=True)
    collector_user_id = mapped_column(BIGINT, nullable=True)
    collector_name = mapped_column(String, nullable=True)
    message_chat_id = mapped_column(BIGINT, nullable=True)
    message_id = mapped_column(BIGINT, nullable=True)
    message_has_photo = mapped_column(Boolean, nullable=False, default=False)
    report_message_chat_id = mapped_column(BIGINT, nullable=True)
    report_message_id = mapped_column(BIGINT, nullable=True)
    report_message_has_photo = mapped_column(Boolean, nullable=False, default=False)
    created_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
    placed_at = mapped_column(DateTime, nullable=True)
    sheet_added_at = mapped_column(DateTime, nullable=True)
    sheet_row = mapped_column(Integer, nullable=True)
    sheet_amount_before = mapped_column(Integer, nullable=True)
    sheet_amount_after = mapped_column(Integer, nullable=True)
    sheet_error = mapped_column(Text, nullable=True)
    collector_sheet_added_at = mapped_column(DateTime, nullable=True)
    collector_sheet_row = mapped_column(Integer, nullable=True)
    collector_sheet_error = mapped_column(Text, nullable=True)
