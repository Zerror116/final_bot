import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    String,
    BIGINT,
    DateTime,
    Index,
    Integer,
)
from sqlalchemy.orm import mapped_column, Session
from .db import AbstractModel, engine

SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.datetime.now(SAMARA_TZ).replace(tzinfo=None)


class InDelivery(AbstractModel):
    __tablename__ = "in_delivery"
    __table_args__ = (
        Index("ix_in_delivery_user_id", "user_id"),
        Index("ix_in_delivery_post_id", "post_id"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    reservation_id = mapped_column(Integer, nullable=True)
    post_id = mapped_column(Integer, nullable=False)
    user_id = mapped_column(BIGINT, nullable=False)
    user_name = mapped_column(String, nullable=False)
    item_description = mapped_column(String, nullable=False)
    quantity = mapped_column(Integer, nullable=False)
    price = mapped_column(Integer, nullable=False)
    delivery_address = mapped_column(String, nullable=False)
    data = mapped_column(DateTime, nullable=False, default=local_now_naive)

    @staticmethod
    def insert(post_id, user_id, user_name, item_description, quantity, price, delivery_address, reservation_id=None):
        """
        Добавляет запись в таблицу in_delivery.
        """
        with Session(bind=engine) as session:
            try:
                new_entry = InDelivery(
                    reservation_id=reservation_id,
                    post_id=post_id,
                    user_id=user_id,
                    user_name=user_name,
                    item_description=item_description,
                    quantity=quantity,
                    price=price,
                    delivery_address=delivery_address
                )
                session.add(new_entry)
                session.commit()
            except Exception as e:
                session.rollback()
                raise e

    @staticmethod
    def get_all_rows():
        """
        Возвращает все записи из in_delivery.
        """
        with Session(bind=engine) as session:
            try:
                return session.query(InDelivery).all()
            except Exception as e:
                session.rollback()
                raise e

    @staticmethod
    def clear_table():
        """
        Очищает таблицу in_delivery.
        """
        with Session(bind=engine) as session:
            try:
                session.query(InDelivery).delete()
                session.commit()
            except Exception as e:
                session.rollback()
                raise e
