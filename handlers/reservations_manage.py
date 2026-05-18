from sqlalchemy import func

from db import Reservations, Posts
from db.db import engine
from sqlalchemy.orm import Session

# Вспомогательные функции для вычислений
def calculate_total_sum(user_id):
    """
    Рассчитывает общую сумму заказов пользователя.
    """
    with Session(bind=engine) as session:
        total = session.query(
            func.sum(
                (Posts.price * Reservations.quantity)
                - func.coalesce(Reservations.return_order, 0)
            )
        ).select_from(
            Reservations
        ).join(
            Posts, Posts.id == Reservations.post_id
        ).filter(
            Reservations.user_id == user_id
        ).scalar()
    return int(total or 0)

def calculate_processed_sum(user_id):
    """
    Рассчитывает сумму обработанных заказов пользователя.
    """
    with Session(bind=engine) as session:
        total = session.query(
            func.sum(
                (Posts.price * Reservations.quantity)
                - func.coalesce(Reservations.return_order, 0)
            )
        ).select_from(
            Reservations
        ).join(
            Posts, Posts.id == Reservations.post_id
        ).filter(
            Reservations.user_id == user_id,
            Reservations.is_fulfilled == True,
        ).scalar()
    return int(total or 0)

