import sqlite3

# 你的資料庫名稱（如果不確定，可以去 app.py 裡找 get_db() 用的那個）
DB_PATH = "database.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# === 建立三張表（若不存在）===
cur.executescript("""
-- posts: 貼文
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  content TEXT NOT NULL,
  image_path TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- comments: 留言
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- likes: 按讚（同一用戶對同一貼文只能讚一次）
CREATE TABLE IF NOT EXISTS likes (
  user_id INTEGER NOT NULL,
  post_id INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, post_id)
);
""")

conn.commit()
conn.close()
print("✅ 已建立 posts / comments / likes 三張表。")
