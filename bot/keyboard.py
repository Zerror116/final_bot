from telebot.types import ReplyKeyboardMarkup, KeyboardButton



def supreme_leader_main_menu():
    leader_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    create_new_post = KeyboardButton("➕ Новый пост")
    manage_posts = KeyboardButton("📄 Посты")
    send_new_posts_to_channel = KeyboardButton("📢 Отправить посты в канал")
    my_orders = KeyboardButton("🛒 Мои заказы")
    manage_clients = KeyboardButton("⚙️ Клиенты")
    delivery_management = KeyboardButton("🚚 Управление доставкой")
    orders_in_delivery = KeyboardButton("🚗 Заказы в доставке")
    manage_workers = KeyboardButton("👔 Назначить работника")
    audit_manage = KeyboardButton(" Ревизия")
    i_have_a_defect = KeyboardButton("😞 У меня брак")
    phoenix_broadcast = KeyboardButton("Рассылка о Фениксе")
    leader_keyboard.add(create_new_post, manage_posts, send_new_posts_to_channel, my_orders, manage_clients,
                        delivery_management, orders_in_delivery, manage_workers, audit_manage, i_have_a_defect,
                        phoenix_broadcast)
    return leader_keyboard

def client_main_menu():
    client_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    my_orders = KeyboardButton("🛒 Мои заказы")
    orders_in_delivery = KeyboardButton("🚗 Заказы в доставке")
    i_have_a_defect = KeyboardButton("😞 У меня брак")
    client_keyboard.add(my_orders, orders_in_delivery, i_have_a_defect)
    return client_keyboard

def worker_main_menu():
    worker_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    create_new_post = KeyboardButton("➕ Новый пост")
    my_orders = KeyboardButton("🛒 Мои заказы")
    manage_posts = KeyboardButton("📄 Посты")
    worker_keyboard.add(create_new_post, my_orders, manage_posts)
    return worker_keyboard

def audit_main_menu():
    audit_keydoard = ReplyKeyboardMarkup(resize_keyboard=True)
    create_new_post = KeyboardButton("➕ Новый пост")
    my_orders = KeyboardButton("🛒 Мои заказы")
    manage_posts = KeyboardButton("📄 Посты")
    audit_manage = KeyboardButton(" Ревизия")
    audit_keydoard.add(create_new_post, my_orders, manage_posts, audit_manage)
    return audit_keydoard

def admin_main_menu():
    admin_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    my_orders = KeyboardButton("🛒 Мои заказы")
    manage_posts = KeyboardButton("📄 Посты")
    send_new_posts_to_channel = KeyboardButton("📢 Отправить посты в канал")
    manage_clients = KeyboardButton("⚙️ Клиенты")
    delivery_management = KeyboardButton("🚚 Управление доставкой")
    manage_workres = KeyboardButton("👔 Назначить работника")
    admin_keyboard.add( my_orders,manage_posts, send_new_posts_to_channel, manage_clients,
                 delivery_management,manage_workres)
    return admin_keyboard
def unknown_main_menu():
    unknown_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    registration_button = KeyboardButton("Регистрация")
    unknown_keyboard.add(registration_button)
    return unknown_keyboard
