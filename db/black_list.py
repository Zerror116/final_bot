from sqlalchemy import BIGINT, Boolean, Index, Integer, String, text
from sqlalchemy.orm import mapped_column, Session

from .db import AbstractModel, engine

class BlackList(AbstractModel):
    __tablename__ = "black_list"
    __table_args__ = (
        Index("ix_black_list_user_id", "user_id"),
        Index("ix_black_list_phone", "phone"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(BIGINT, nullable=False)
    phone = mapped_column(String, nullable=False)
    silent_block = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    @staticmethod
    def insert(user_id: int, phone: str = "", silent_block: bool = False):
        with Session(bind=engine) as session:
            try:
                rows = session.query(BlackList).filter(BlackList.user_id == user_id).all()
                if rows:
                    for entry in rows:
                        if phone:
                            entry.phone = phone
                        entry.silent_block = bool(entry.silent_block or silent_block)
                else:
                    entry = BlackList(
                        user_id=user_id,
                        phone=phone or "",
                        silent_block=bool(silent_block),
                    )
                    session.add(entry)
                session.commit()
            except Exception:
                raise

    @staticmethod
    def get_row(user_id: int):
        with Session(bind=engine) as session:
            query = session.query(BlackList).filter(BlackList.user_id == user_id).all()
            return query

    @staticmethod
    def set_silent_block(user_id: int, phone: str = ""):
        BlackList.insert(user_id=user_id, phone=phone, silent_block=True)

    @staticmethod
    def is_silent_blocked(user_id: int) -> bool:
        with Session(bind=engine) as session:
            return session.query(BlackList.id).filter(
                BlackList.user_id == user_id,
                BlackList.silent_block == True,
            ).first() is not None
