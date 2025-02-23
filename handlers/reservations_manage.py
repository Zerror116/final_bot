from db import Reservations, Posts


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

# Вспомогательные функции для вычислений
def calculate_total_sum(user_id):
    """
    Рассчитывает общую сумму заказов пользователя.
    """
    reservations = Reservations.get_row_by_user_id(user_id)
    if not reservations:
        return 0

    total_sum = 0
    for reservation in reservations:
        post = Posts.get_row_by_id(reservation.post_id)
        if post:
            total_sum += post.price

    return total_sum


def calculate_processed_sum(user_id):
    """
    Рассчитывает сумму обработанных заказов пользователя.
    """
    reservations = Reservations.get_row_by_user_id(user_id)
    if not reservations:
        print(f"Нет бронирований пользователя с ID: {user_id}")
        return 0

    processed_sum = 0
    for reservation in reservations:
        print(f"Бронирование ID {reservation.id}, статус: {reservation.is_fulfilled}")
        if reservation.is_fulfilled:  # Если is_fulfilled — булево значение
            post = Posts.get_row_by_id(reservation.post_id)
            if post:
                print(
                    f"Добавляем сумму {post.price} для поста ID {reservation.post_id}"
                )
                processed_sum += post.price
            else:
                print(f"Нет данных в Posts для post_id: {reservation.post_id}")
        else:
            print(f"Бронирование ID {reservation.id} не обработано.")
    print(f"Общая сумма обработанных заказов: {processed_sum}")
    return processed_sum

