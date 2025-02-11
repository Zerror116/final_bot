from db import Reservations
# from db import engine, Session


def delete_cart(user_id: int):
    """Очистка корзины клиента."""
    try:
        # with Session(bind=engine) as session:
        #     reservations = session.query(Reservations).filter(Reservations.user_id == user_id).all()
        reservations = Reservations.get_row_all(user_id=user_id)
        if not reservations:
            print(f"Корзина для клиента с ID {user_id} не найдена.")
            return
        for reservation in reservations:
            status = Reservations.delete_row(reservation_id=reservation.id)
            if not status:
                continue
            # session.delete(reservation)
        # session.commit()
        print(f"Корзина клиента с ID {user_id} успешно очищена.")
    except Exception as e:
        print(f"Ошибка при очистке корзины: {e}")


def get_all_reservations(user_id: int):
    """Получение всех бронирований клиента."""
    try:
        reservations = Reservations.get_row_all(user_id=user_id)
        if not reservations:
            print(f"Бронирования для клиента с ID {user_id} отсутствуют.")
            return
        for reservation in reservations:
            print(
                f"Бронирование ID {reservation.id}: Кол-во: {reservation.quantity}, Пост ID: {reservation.post_id}, Выполнено: {reservation.is_fulfilled}")
    except Exception as e:
        print(f"Ошибка при получении бронирований: {e}")
