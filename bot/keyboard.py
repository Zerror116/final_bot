from telebot.types import ReplyKeyboardMarkup, KeyboardButton

def supreme_leader_main_menu():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    create_new_post = KeyboardButton("➕ Новый пост")
    manage_posts = KeyboardButton("📄 Посты")
    send_new_posts_to_channel = KeyboardButton("📢 Отправить посты в канал")
    my_orders = KeyboardButton("🛒 Мои заказы")
    manage_clients = KeyboardButton("⚙️ Клиенты")
    send_all_reserved_to_group = KeyboardButton("📦 Заказы клиентов")
    keyboard.add(create_new_post, manage_posts, send_new_posts_to_channel, my_orders, manage_clients, send_all_reserved_to_group)
    return keyboard

def client_main_menu():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    my_orders = KeyboardButton("🛒 Мои заказы")
    keyboard.add(my_orders)
    return keyboard

def worker_main_menu():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    create_new_post = KeyboardButton("➕ Новый пост")
    my_orders = KeyboardButton("🛒 Мои заказы")
    manage_posts = KeyboardButton("📄 Посты")
    keyboard.add(create_new_post, my_orders, manage_posts)
    return keyboard

def admin_main_menu():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    create_new_post = KeyboardButton("➕ Новый пост")
    manage_posts = KeyboardButton("📄 Посты")
    send_new_posts_to_channel = KeyboardButton("📢 Отправить посты в канал")
    my_orders = KeyboardButton("🛒 Мои заказы")
    manage_clients = KeyboardButton("⚙️ Клиенты")
    send_all_reserved_to_group = KeyboardButton("📦 Заказы клиентов")
    keyboard.add(create_new_post, manage_posts, send_new_posts_to_channel, my_orders, manage_clients,
                 send_all_reserved_to_group)
    return keyboard
def unknown_main_menu():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    registration_button = KeyboardButton("Регистрация")
    keyboard.add(registration_button)
    return keyboard