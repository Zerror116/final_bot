from .posts import Posts
from .clients import Clients
from .black_list import BlackList
from .reservations import Reservations
from .temp_reservations import TempReservations
from .db import AbstractModel, engine
from sqlalchemy.orm import mapped_column, Session

# AbstractModel.metadata.drop_all(engine)

AbstractModel.metadata.create_all(engine)

__all__ = {"Posts", "Clients", "BlackList", "Reservations", "TempReservations"}

print('Запуск')