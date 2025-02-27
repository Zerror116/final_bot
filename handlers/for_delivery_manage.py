from db.for_delivery import ForDelivery
from db import engine, Session, Posts

def add_to_for_delivery(user_id, name, phone, address, total_sum):
    """
    Добавляет клиента в список for_delivery после согласия на доставку.
    """
    try:
        ForDelivery.insert(user_id, name, phone, address, total_sum)
    except Exception as e:
        raise Exception(f"Ошибка при добавлении в for_delivery: {e}")
