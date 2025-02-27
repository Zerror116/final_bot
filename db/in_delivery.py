import datetime
from ast import Bytes

from sqlalchemy import (
    String,
    ForeignKey,
    BIGINT,
    Boolean,
    DateTime,
    or_, Integer,
    BLOB
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import mapped_column, Session, Mapped, MappedColumn

from . import Posts
# from main import user_data
from .db import AbstractModel, engine

class InDelivery(AbstractModel):
    __tablename__ = "in_delivery"
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(BIGINT, nullable=False)
    item_description = mapped_column(String, nullable=False)
    quantity = mapped_column(Integer, nullable=False)
    total_sum = mapped_column(Integer, nullable=False)
    delivery_address = mapped_column(String, nullable=False)
    data = mapped_column(DateTime, nullable=False, default=datetime.datetime.now())

    @staticmethod
    def insert(user_id, item_description, quantity, total_sum, delivery_address):
        """
        Добавляет запись в таблицу in_delivery.
        """
        with Session(bind=engine) as session:
            try:
                new_entry = InDelivery(
                    user_id=user_id,
                    item_description=item_description,
                    quantity=quantity,
                    total_sum=total_sum,
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
            return session.query(InDelivery).all()

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

