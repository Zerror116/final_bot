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

class ForDelivery(AbstractModel):
    __tablename__ = "for_delivery"
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone = mapped_column(String, nullable=False)
    name = mapped_column(String, nullable=False)
    total_sum = mapped_column(Integer, nullable=False)
    address = mapped_column(String, nullable=False)
    user_id = mapped_column(BIGINT, nullable=False)
    @staticmethod
    def insert(user_id, name, phone, address, total_sum):
        with Session(bind=engine) as session:
            try:
                new_entry = ForDelivery(
                    user_id=user_id,
                    name=name,
                    phone=phone,
                    address=address,
                    total_sum=total_sum
                )
                session.add(new_entry)
                session.commit()  # Подтверждаем изменения
            except Exception as e:
                session.rollback()  # В случае ошибки откатываем изменения
                raise e

    @staticmethod
    def get_row(user_id: int):
        with Session(bind=engine) as session:
            query = session.query(ForDelivery).filter(ForDelivery.user_id == user_id).all()
            return query

    # @staticmethod
    # def update_row(post_id: int, price: int, description: str, quantity: int):
    #
    # @staticmethod
    # def delete_row():