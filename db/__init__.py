from .posts import Posts
from .clients import Clients
from .black_list import BlackList
from .reservations import Reservations
from .for_delivery import ForDelivery
from .temp_fulfilied import Temp_Fulfilled
from .in_delivery import InDelivery
from .temp_reservations import TempReservations
from .db import AbstractModel, engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

def init_db():
    AbstractModel.metadata.create_all(engine)
    ensure_reservations_created_at()


def ensure_reservations_created_at():
    inspector = inspect(engine)
    if "reservations" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("reservations")}
    if "created_at" in columns:
        return

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE reservations "
                    "ADD COLUMN created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()"
                )
            )
        else:
            connection.execute(text("ALTER TABLE reservations ADD COLUMN created_at DATETIME"))
            connection.execute(
                text("UPDATE reservations SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
            )

__all__ = {"Posts", "Clients", "BlackList", "Reservations", "TempReservations", "ForDelivery", "InDelivery", "Temp_Fulfilled", "init_db"}
