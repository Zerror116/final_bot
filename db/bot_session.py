import json

from sqlalchemy import BIGINT, DateTime, Text, func
from sqlalchemy.orm import Session, mapped_column

from .db import AbstractModel, engine


class BotSession(AbstractModel):
    __tablename__ = "bot_sessions"

    user_id = mapped_column(BIGINT, primary_key=True)
    state_json = mapped_column(Text, nullable=True)
    temp_user_data_json = mapped_column(Text, nullable=True)
    temp_post_data_json = mapped_column(Text, nullable=True)
    user_data_json = mapped_column(Text, nullable=True)
    updated_at = mapped_column(DateTime, nullable=False, default=func.now(), onupdate=func.now())

    BUCKET_COLUMNS = {
        "temp_user_data": "temp_user_data_json",
        "temp_post_data": "temp_post_data_json",
        "user_data": "user_data_json",
    }

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def decode(value):
        if value is None or value == "":
            return None
        return json.loads(value)

    @staticmethod
    def _get_or_create(session, user_id):
        row = session.query(BotSession).filter(BotSession.user_id == int(user_id)).first()
        if row:
            return row

        row = BotSession(user_id=int(user_id))
        session.add(row)
        return row

    @staticmethod
    def get_state(user_id):
        with Session(bind=engine) as session:
            row = session.query(BotSession).filter(BotSession.user_id == int(user_id)).first()
            return BotSession.decode(row.state_json) if row else None

    @staticmethod
    def set_state(user_id, state):
        with Session(bind=engine) as session:
            row = BotSession._get_or_create(session, user_id)
            row.state_json = BotSession.encode(state)
            session.commit()

    @staticmethod
    def clear_state(user_id):
        with Session(bind=engine) as session:
            row = session.query(BotSession).filter(BotSession.user_id == int(user_id)).first()
            if row:
                row.state_json = None
                session.commit()

    @staticmethod
    def get_bucket(user_id, bucket_name):
        column = BotSession.BUCKET_COLUMNS[bucket_name]
        with Session(bind=engine) as session:
            row = session.query(BotSession).filter(BotSession.user_id == int(user_id)).first()
            return BotSession.decode(getattr(row, column)) if row else None

    @staticmethod
    def set_bucket(user_id, bucket_name, value):
        column = BotSession.BUCKET_COLUMNS[bucket_name]
        with Session(bind=engine) as session:
            row = BotSession._get_or_create(session, user_id)
            setattr(row, column, BotSession.encode(value))
            session.commit()

    @staticmethod
    def clear_bucket(user_id, bucket_name):
        column = BotSession.BUCKET_COLUMNS[bucket_name]
        with Session(bind=engine) as session:
            row = session.query(BotSession).filter(BotSession.user_id == int(user_id)).first()
            if row:
                setattr(row, column, None)
                session.commit()
