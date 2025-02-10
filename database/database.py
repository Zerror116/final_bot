import sqlite3

DB_NAME = "bot_database.db"

# TODO pomeniay ety huiny
def init_db():
    """Инициализация базы данных."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Создаем таблицу для постов
    # Таблица для постов
    cursor.execute('''CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        photo TEXT,
        price TEXT,
        description TEXT,
        message_id INTEGER,
        quantity INTEGER
    )''')

    # Таблица для клиентов
    cursor.execute('''CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        name TEXT,
        phone TEXT,
        role TEXT
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS black_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE,
    phone TEXT
    )''')

    # Таблица для заказов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            quantity INTEGER DEFAULT 0,
            post_id INTEGER,
            is_fulfilled INTEGER DEFAULT 0, -- Новый столбец
            FOREIGN KEY (user_id) REFERENCES clients (user_id),
            FOREIGN KEY (post_id) REFERENCES posts (id)
        )
    ''')

    conn.commit()



    # Убеждаемся, что столбец 'is_sent' существует, если база уже создана
    cursor.execute("PRAGMA table_info(posts)")
    columns = [col[1] for col in cursor.fetchall()]
    if "is_sent" not in columns:
        cursor.execute("""
            ALTER TABLE posts ADD COLUMN is_sent INTEGER DEFAULT 0
            """)

    conn.commit()
    conn.close()

def update_db_schema():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('ALTER TABLE reservations ADD COLUMN is_fulfilled BOOLEAN DEFAULT 0')
    conn.commit()
    conn.close()


# Вызов функции при инициализации базы данных
try:
    update_db_schema()
except sqlite3.OperationalError:
    pass  # Если столбец уже существует, игнорируем ошибку


def save_post(chat_id, photo, price, description, quantity):
    """Сохранение поста в базу данных."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO posts (chat_id, photo, price, description, quantity)
    VALUES (?, ?, ?, ?, ?)
    """, (chat_id, photo, price, description, quantity))

    conn.commit()
    conn.close()


def get_posts(chat_id):
    """Получение всех постов для указанного chat_id."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, photo, price, description, quantity
    FROM posts
    WHERE chat_id = ?
    """, (chat_id,))

    posts = cursor.fetchall()
    conn.close()
    return posts


def delete_post_by_id(post_id):
    """Удаление поста по ID."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    DELETE FROM posts
    WHERE id = ?
    """, (post_id,))
    conn.commit()
    conn.close()


# >>> Изменение <<<: Добавлена функция для обновления поста
def update_post_by_id(post_id, price, description, quantity):
    """Обновление поста по ID."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    UPDATE posts
    SET price = ?, description = ?, quantity = ?
    WHERE id = ?
    """, (price, description, quantity, post_id))

    conn.commit()
    conn.close()
