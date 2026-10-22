from db import Posts


# Сохранение поста
def save_post(chat_id: int, photo: str, price: str, description: str, quantity: int, post_id: int = None):
    """Сохранение поста в базу данных."""
    return Posts.insert(
        chat_id=chat_id,
        photo=photo,
        price=price,
        description=description,
        quantity=quantity,
        post_id=post_id,
    )



