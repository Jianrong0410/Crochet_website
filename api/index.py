from flask import Flask, request, render_template, url_for, redirect, flash, session, jsonify
from flask import render_template_string  # ← 新增：列表頁直接輸出用
import os
from ultralytics import YOLO
import cv2
import json
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import time  # ← 新增：產生唯一檔名用
# ==== 忘記密碼用 ====
# 時間工具（驗證碼有效期）、隨機驗證碼、SMTP 寄信
from datetime import datetime, timedelta
import secrets
import smtplib, ssl
from email.mime.text import MIMEText
from email.header import Header
import time
ALLOWED_IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "..", "templates"),
    static_folder=os.path.join(BASE_DIR, "..", "static")
)
# Session 加密金鑰（建議用環境變數設定正式值）
app.secret_key = os.environ.get('FLASK_SECRET', 'dev-secret')  # 建議改環境變數

# -----------------------------
# SMTP 寄信設定（可由環境變數覆蓋）
# -----------------------------
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
MAIL_FROM  = os.environ.get('MAIL_FROM', SMTP_USER)
MAIL_SENDER_NAME = os.environ.get('MAIL_SENDER_NAME', '鉤織圖辨識系統')

def save_image_to_static(file_storage, subdir):
    """
    將上傳的圖存到 static/<subdir>/yyyy/mm/ 檔名隨機，回傳相對 static 的路徑字串
    例：post_images/2025/11/1730541234_a1b2c3.png
    """
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
        raise ValueError('不支援的圖片格式')

    y = time.strftime('%Y')
    m = time.strftime('%m')
    rel_dir = os.path.join(subdir, y, m)                 # 相對 static
    abs_dir = os.path.join('static', rel_dir)            # 實體目錄
    os.makedirs(abs_dir, exist_ok=True)

    rand = secrets.token_hex(6)
    ts = str(int(time.time()))
    filename = f'{ts}_{rand}{ext}'
    abs_path = os.path.join(abs_dir, filename)
    file_storage.save(abs_path)

    return os.path.join(rel_dir, filename).replace('\\', '/')

def send_code_email(to_email: str, code: str):
    """寄出 6 位數驗證碼。若未設定 SMTP 帳密，退而印在主控台（本機測試用）。"""
    subject = "重設密碼驗證碼"
    body = f"""您好，

您正在進行「重設密碼」操作。
請在 10 分鐘內輸入以下 6 位數驗證碼完成重設：

驗證碼：{code}

若非本人操作，請忽略本信。

—— {MAIL_SENDER_NAME}
"""
    # 若未設定 SMTP 帳密，直接在cmd印出驗證碼
    if not SMTP_USER or not SMTP_PASS:
        print("【開發模式】未設定 SMTP_USER/SMTP_PASS，以下為驗證碼：", code)
        return
    # 建立純文字郵件內容（UTF-8）
    msg = MIMEText(body, _charset='utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    # 發件人顯示名稱 + Email
    sender = f"{MAIL_SENDER_NAME} <{MAIL_FROM}>"
    msg['From'] = sender
    msg['To'] = to_email
    # 建立 TLS 加密連線並寄出
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(MAIL_FROM, [to_email], msg.as_string())

def gen_6_code():
    # 產生 6 位數字字串（前面補零）
    return f"{secrets.randbelow(1_000_000):06d}"

# -----------------------------
# SQLite：users + results + password_resets
# -----------------------------
# SQLite 資料庫檔案路徑（與 index.py 同資料夾）
DB_PATH = os.path.join(os.path.dirname(__file__), 'index.db')

def get_db():
    """取得資料庫連線，並讓查詢結果可用欄位名存取（Row）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ---- 放在 index = Flask(...) 之後、各個 route 之前 ----
def ensure_social_tables():
    conn = get_db()
    # posts：允許文字 + 圖片
    conn.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            image_path TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    # comments：留言
    conn.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    # likes：按讚（唯一性：同一人對同一貼文只能讚一次）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS likes (
            user_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(user_id, post_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (post_id) REFERENCES posts(id)
        )
    ''')
    # follows：追蹤/粉絲
    conn.execute('''
        CREATE TABLE IF NOT EXISTS follows (
            follower_id INTEGER NOT NULL,
            followee_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(follower_id, followee_id),
            FOREIGN KEY (follower_id) REFERENCES users(id),
            FOREIGN KEY (followee_id) REFERENCES users(id)
        )
    ''')
    conn.commit()
    conn.close()

# ✅ Flask 3.x：啟動階段建表
with app.app_context():
    ensure_social_tables()

def init_db():
    """初始化資料庫：建立 users、results、password_resets 三個資料表（若不存在）。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    # 使用者表：儲存名稱、Email（唯一）、密碼雜湊、建立時間    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at DATETIME DEFAULT (datetime('now','localtime'))
        )
    ''')
    # 結果表：儲存使用者偵測結果（原檔名、輸入/偵測後圖片路徑、物件數量 JSON、物件清單、時間）
    conn.execute('''
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            original_filename TEXT,
            input_image TEXT,
            detected_image TEXT,
            counts_json TEXT,
            objects_text TEXT,
            created_at DATETIME DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    # 忘記密碼表：儲存驗證碼與有效期限
    conn.execute('''
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            created_at DATETIME DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'bio' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
    if 'avatar_path' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT DEFAULT ''")
    conn.commit()
    conn.close()

def login_required(view_func):
    """登入保護裝飾器：未登入者導向 login 頁並提示。"""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            flash('請先登入。')
            return redirect(url_for('login'))
        return view_func(*args, **kwargs)
    return wrapped

# -----------------------------
# YOLO 模型與繪圖
# -----------------------------
# 載入 YOLO 權重（best.pt 需放在專案中）
model = YOLO("best.pt")
# 顏色對照表檔案（每個類別固定顏色，避免每次重啟顏色變動）
COLOR_FILE = "color_map.json"

# 啟動時若已有顏色對照檔就載入，否則建立空表
if os.path.exists(COLOR_FILE):
    with open(COLOR_FILE, "r", encoding="utf-8") as f:
        color_map = json.load(f)
else:
    color_map = {}

def generate_color(name):
    """依類別名稱產生穩定的隨機色（使用名稱的雜湊作為種子）。"""
    import random
    random.seed(hash(name))
    return tuple(int(random.uniform(50, 180)) for _ in range(3))

# 英文對中文名稱
name_mapping = {
    "ch": "鎖針(ch)",
    "sc": "短針(sc)",
    "dc": "長針(dc)",
    "sl_st": "引拔針(sl_st)",
    "hdc": "中長針(hdc)",
    "tr": "長長針(tr)",
    "dtr": "三卷長針(dtr)",
    "2hdc": "中長加針(2hdc)",
    "3hdc": "中長三加針(3hdc)",
    "2dc": "長加針(2dc)",
    "2sc": "短針加針(2sc)",
    "3dc": "長三加針(3dc)",
    "3sc": "短針三加針(3sc)",
    "4dc": "長四加針(4dc)",
    "5dc": "長五加針(5dc)",
    "7dc": "長七加針(7dc)",
    "2tr": "長長加針(2tr)",
    "3tr": "長長三加針(3tr)",
    "3dtr": "三卷長針三加針(3dtr)",
    "hdc2tog": "中長針減針(hdc2tog)",
    "hdc3tog": "中長針減三針(hdc3tog)",
    "dc2tog": "長針減針(dc2tog)",
    "sc2tog": "短針減針(sc2tog)",
    "sc3tog": "短針減三針(sc3tog)",
    "dc3tog": "長針減三針(dc3tog)",
    "dc4tog": "長針減四針(dc4tog)",
    "tr2tog": "長長針減針(tr2tog)",
    "tr3tog": "長長針減三針(tr3tog)",
    "5_dc_popcorn": "爆米花針(5_dc_popcorn)",
    "5_hdc_popcorn": "五針中長針爆米花針(5_hdc_popcorn)",
    "2_dc_cluster": "長針兩針棗形針(2_dc_cluster)",
    "3_dc_cluster": "長針三針棗形針(3_dc_cluster)",
    "4_dc_cluster": "長針四針棗形針(4_dc_cluster)",
    "5_dc_cluster": "長針五針棗形針(5_dc_cluster)",
    "3_hdc_cluster": "中長針三針棗形針 / puff(3_hdc_cluster)",
    "4_hdc_cluster": "中長針四針棗形針(4_hdc_cluster)",
    "3_tr_cluster": "長長針三針棗形針(3_tr_cluster)",
    "5_tr_cluster": "長長針五針棗形針(5_tr_cluster)",
    "6_tr_cluster": "長長針六針棗形針(6_tr_cluster)",
    "FPsc": "外鉤短針 右凸起針(FPsc)",
    "BPsc": "內鉤短針針(BPsc)",
    "FPhdc": "外鉤中長針(FPhdc)",
    "BPhdc": "內鉤中長針(BPhdc)",
    "FPdc": "外鉤長針(FPdc)",
    "BPdc": "內鉤長針(BPdc)",
    "FPtr": "外鉤長長針(FPtr)",
    "BPtr": "內鉤長長針(BPtr)",
    "ch-3_picot": "鎖三針的狗牙拉針(有一點)(ch-3_picot)",
    "ch-3_sc_picot": "鎖三針的短針狗牙針(有叉叉)(ch-3_sc_picot)",
    "5_dc_shell": "貝殼針(5_dc_shell)",
    "3ch_2dc": "3起立針2長針(3ch_2dc)",
    "2ch_2_dc_cluster": "2起立針2長針(2ch_2_dc_cluster)",
}

def predict_image(img_path, username):
    """
    讀取圖片做 YOLO 偵測，畫上方框與標籤，統計各類別數量，
    並把結果圖輸出到 static/results/<username>/ 底下。
    回傳： (counts_dict, 'results/<username>/<檔名>')
    """
    results = model(img_path) # 執行 YOLO 偵測
    os.makedirs(f"static/results/{username}", exist_ok=True)
    img = cv2.imread(img_path)
    height, width, _ = img.shape
    thickness = max(1, int(width*0.002)) # 線條粗細依圖寬調整
    counts = {} # 計數各類別出現次數

    # 若有偵測到物件才進一步處理
    if len(results[0].boxes) > 0:
        boxes = results[0].boxes.xyxy.cpu().numpy() # 取出偵測框座標 (x1,y1,x2,y2)
        classes = results[0].boxes.cls.cpu().numpy()# 取出每個框對應的類別 id
        for box, cls_id in zip(boxes, classes):
            cls_id = int(cls_id)
            name = model.names[cls_id]               # 類別英文代號（來自模型）
            counts[name] = counts.get(name, 0) + 1   # 計數
            
            # 若該類別尚未配置顏色，動態產生並存入 color_map
            if name not in color_map:
                color_map[name] = generate_color(name)
            color = tuple(color_map[name])
            # 繪製方框與標籤
            x1,y1,x2,y2 = map(int, box)
            cv2.rectangle(img,(x1,y1),(x2,y2),color,thickness)
            font = cv2.FONT_HERSHEY_SIMPLEX
            # 依框大小調整字體尺寸與粗細
            font_scale = max(0.3, min(0.9, (x2-x1)/250))
            font_thickness = max(1, thickness//2)
            text = name
            (text_w,text_h),_ = cv2.getTextSize(text,font,font_scale,font_thickness)
            label_x,label_y = x1+3, y1+text_h+3
            # 先描黑邊讓字更清楚，再畫彩色字
            cv2.putText(img,text,(label_x,label_y),font,font_scale,(0,0,0),font_thickness+2,cv2.LINE_AA)
            cv2.putText(img,text,(label_x,label_y),font,font_scale,color,font_thickness,cv2.LINE_AA)

    # 每次偵測後都把 color_map 寫回檔案（保持顏色一致性）
    with open(COLOR_FILE, "w", encoding="utf-8") as f:
        json.dump(color_map, f, ensure_ascii=False)

    # 寫出偵測後圖片至使用者專屬資料夾
    output_path = f"static/results/{username}/" + os.path.basename(img_path)
    cv2.imwrite(output_path, img)
    # 回傳：各類別次數 dict、以及結果圖片在 static 下的相對路徑（給前端組 URL）
    return counts, f"results/{username}/{os.path.basename(img_path)}"

def save_image_to_static(file_obj, subdir):
    """
    將上傳的圖檔存到 static/<subdir>/<user_id>/ 目錄下，檔名加上 timestamp，回傳相對 static 路徑。
    """
    user_id = session['user_id']
    ext = os.path.splitext(file_obj.filename)[1].lower() or '.jpg'
    folder = os.path.join('static', subdir, str(user_id))
    os.makedirs(folder, exist_ok=True)
    fname = f"{int(time.time()*1000)}{ext}"
    save_path = os.path.join(folder, fname)
    file_obj.save(save_path)
    # 回傳相對 static 的路徑（存 DB 用）
    return f"{subdir}/{user_id}/{fname}"

# -----------------------------
# 工具：正規化相對 static 的路徑
# -----------------------------
def normalize_static_relpath(p: str) -> str:
    """
    接受各種形式的路徑並「轉成相對 static 的路徑」：
    - "static/uploads/..." -> 轉為 "uploads/..."
    - "/static/uploads/..." -> 轉為 "uploads/..."
    - "uploads/..." 或 "results/..." -> 已是相對路徑，原樣返回
    其他（空或 None） -> 回傳空字串
    目的：確保儲存在資料庫的路徑是「相對 static/」的乾淨形式，方便用 url_for('static', filename=...) 組 URL。
    """
    if not p:
        return ''
    p = p.strip().lstrip('/')       # 去除空白與開頭的斜線
    if p.startswith('static/'):
        return p[len('static/'):]   # 去掉前綴 'static/'
    return p

# -----------------------------
# 頁面路由
# -----------------------------
@app.route('/')
@login_required
def index():
    # 從 session 取得當前登入的使用者名稱
    username = session.get('user_name', '')
    return render_template('index.html', username=username)

@app.route('/predict', methods=['POST'])
@login_required
def predict():
    # 檢查是否有上傳檔案（表單欄位名：file）
    if 'file' not in request.files or request.files['file'].filename=='':
        return "No file uploaded",400
    file = request.files['file']        # 取得檔案物件
    username = session.get('user_name') # 取得使用者名稱（用來建使用者專屬目錄）
    # 依使用者建立上傳目錄：
    user_upload_dir = f"static/uploads/{username}"
    os.makedirs(user_upload_dir, exist_ok=True)
    # 實體儲存路徑：
    file_path = os.path.join(user_upload_dir, file.filename)
    file.save(file_path)

    counts, result_image = predict_image(file_path, username)
    parts = []
    for eng_name, cnt in counts.items():
        display_name = name_mapping.get(eng_name, eng_name)
        parts.append(f"{display_name}：{cnt}個")
    objects_text = "、".join(parts) if parts else "未偵測到物件"

    # 依表單勾選決定是否顯示原圖/圈選結果，是否儲存到歷史
    show_original = "output_original" in request.form
    show_circle = "output_circle" in request.form
    save_history_flag = "save_history" in request.form

   # 若使用者勾選「儲存歷史紀錄」，則把這次結果寫入 DB
    if save_history_flag:
        conn = get_db()
        conn.execute('''
            INSERT INTO results (user_id, original_filename, input_image, detected_image, counts_json, objects_text)
            VALUES (?,?,?,?,?,?)
        ''', (session['user_id'], file.filename,
              # 這裡存「相對 static/」的路徑（uploads/... 與 results/...）        
              f"uploads/{username}/{file.filename}",
              result_image,
              json.dumps(counts, ensure_ascii=False),
              objects_text))
        conn.commit()
        conn.close()
        flash('已儲存到「我的結果」。')

    return render_template(
        'result.html',
        username=username,
        input_image=url_for('static', filename=f"uploads/{username}/{file.filename}") if show_original else None,
        detected_image=url_for('static', filename=result_image) if show_circle else None,
        objects_text=objects_text,
        counts_json=json.dumps(counts, ensure_ascii=False),
        original_filename=file.filename,
        show_original=show_original,
        show_circle=show_circle
    )

# -------- 儲存結果 --------
@app.route('/save', methods=['POST'])
@login_required
def save_result():
    original_filename = request.form.get('original_filename', '').strip()
    input_image = request.form.get('input_image', '').strip()
    detected_image = request.form.get('detected_image', '').strip()
    objects_text = request.form.get('objects_text', '').strip()
    counts_json = request.form.get('counts_json', '{}').strip()

    # ✅ 關鍵：把前端送來的圖片路徑「正規化為相對 static/」
    # 例如傳來 "/static/uploads/xxx.jpg" → 存成 "uploads/xxx.jpg"
    input_image = normalize_static_relpath(input_image)
    detected_image = normalize_static_relpath(detected_image)

    # 寫入 DB（與 /predict 中的 save_history_path 一致）
    conn = get_db()
    conn.execute('''
        INSERT INTO results (user_id, original_filename, input_image, detected_image, counts_json, objects_text)
        VALUES (?,?,?,?,?,?)
    ''', (session['user_id'], original_filename, input_image, detected_image, counts_json, objects_text))
    conn.commit()
    conn.close()

    flash('已儲存到「我的結果」。')
    return redirect(url_for('history_page'))

# -------- 我的結果 --------
@app.route('/history')
@login_required
def history_page():
    # 取出當前使用者最近 200 筆歷史資料（依 id 由新到舊）
    conn = get_db()
    rows = conn.execute('''
        SELECT id, original_filename, input_image, detected_image, counts_json, objects_text, created_at
        FROM results
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 200
    ''', (session['user_id'],)).fetchall()
    conn.close()

    history = []
    for r in rows:
        # 把 DB 內相對 static 的路徑轉成可直接使用的 URL
        input_image_url = url_for('static', filename=r['input_image']) if r['input_image'] else None
        detected_image_url = url_for('static', filename=r['detected_image']) if r['detected_image'] else None
        try:
            counts = json.loads(r['counts_json']) if r['counts_json'] else {}
        except:
            counts = {}
        history.append({
            "id": r['id'],
            "file_name": r['original_filename'],
            "input_image_url": input_image_url,
            "result_image_url": detected_image_url,
            "counts": counts,
            "show_original": bool(input_image_url),
            "show_circle": bool(detected_image_url),
            "timestamp": r['created_at']
        })
    return render_template('history.html', username=session.get('user_name'), history=history)

@app.route("/history/delete/<int:rid>", methods=["POST"])
@login_required
def delete_history_item(rid):
    conn = get_db()
    conn.execute('DELETE FROM results WHERE id=? AND user_id=?', (rid, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"status":"ok"})

@app.route("/history/rename/<int:rid>", methods=["POST"])
@login_required
def rename_history_item(rid):
    data = request.json
    new_name = data.get("new_name","").strip()
    if not new_name:
        return jsonify({"status":"ok"})
    conn = get_db()
    conn.execute('UPDATE results SET created_at=? WHERE id=? AND user_id=?', (new_name, rid, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"status":"ok"})

@app.route("/history/delete_all", methods=["POST"])
@login_required
def delete_all_history():
    conn = get_db()
    conn.execute('DELETE FROM results WHERE user_id=?', (session['user_id'],))
    conn.commit()
    conn.close()
    return jsonify({"status":"ok"})

# -----------------------------
# 個人介面（Profile）與 修改密碼（Password）
# -----------------------------

@app.route('/profile', methods=['GET'])
@login_required
def profile():
    """
    取得目前登入使用者的基本資料與成果數量，渲染 profile.html。
    - user：id, name, email, created_at, bio, avatar_path
    - total_results：該使用者的結果數量
    """
    conn = get_db()
    user = conn.execute('''
        SELECT id, name, email, created_at, bio, avatar_path
        FROM users
        WHERE id=?
    ''', (session['user_id'],)).fetchone()
    total = conn.execute('SELECT COUNT(*) AS c FROM results WHERE user_id=?',
                         (session['user_id'],)).fetchone()['c']
    # === [ADD] 我按過讚的貼文（最新 20 筆）===
    liked_posts = conn.execute("""
      SELECT p.id, p.content, p.created_at, u.name AS author_name
      FROM likes l
      JOIN posts p ON p.id = l.post_id
      JOIN users u ON u.id = p.user_id
      WHERE l.user_id = ?
      ORDER BY l.created_at DESC
      LIMIT 20
    """, (session['user_id'],)).fetchall()
    my_posts = conn.execute('''
        SELECT id, content, image_path, created_at
        FROM posts
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 50
    ''', (session['user_id'],)).fetchall()

    # === [ADD] 粉絲 / 追蹤中計數 ===
    follower_cnt = conn.execute('SELECT COUNT(*) AS c FROM follows WHERE followee_id=?',
                                (session['user_id'],)).fetchone()['c']
    following_cnt = conn.execute('SELECT COUNT(*) AS c FROM follows WHERE follower_id=?',
                                 (session['user_id'],)).fetchone()['c']

    conn.close()
    return render_template('profile.html',
                           user=user,
                           total_results=total,
                           liked_posts=liked_posts,
                           my_posts=my_posts,
                           # 提供模板使用
                           follower_cnt=follower_cnt,
                           following_cnt=following_cnt)

# === [ADD] 我的粉絲列表（含「移除粉絲」按鈕） ===
@app.get('/profile/followers')
@login_required
def my_followers_page():
    conn = get_db()
    rows = conn.execute('''
        SELECT f.follower_id AS uid, u.name, u.email, u.avatar_path, f.created_at
        FROM follows f
        JOIN users u ON u.id = f.follower_id
        WHERE f.followee_id=?
        ORDER BY f.created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()
    return render_template_string('''
<!doctype html><meta charset="utf-8"><title>我的粉絲</title>
<style>
body{font-family:Arial, sans-serif;max-width:760px;margin:24px auto;padding:0 16px;background:#f7f7f7}
h2{margin:6px 0 16px}
.item{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#fff;border:1px solid #eee;border-radius:10px;padding:12px 14px;margin-bottom:10px}
.left{display:flex;align-items:center;gap:12px}
img{width:44px;height:44px;border-radius:50%;object-fit:cover;background:#ddd}
a.link{color:#6b4aa1;text-decoration:none}
.back{display:inline-block;margin-bottom:12px}
.empty{color:#777}
.btn{background:#ef4444;color:#fff;border:none;border-radius:8px;padding:6px 10px;cursor:pointer}
.actions{display:flex;gap:10px;align-items:center}
.meta{font-size:12px;color:#666}
</style>
<a class="back link" href="{{ url_for('profile') }}">← 回個人介面</a>
<h2>👥 我的粉絲</h2>
{% if rows %}
  {% for r in rows %}
    <div class="item">
      <div class="left">
        <img src="{{ url_for('static', filename=r['avatar_path']) if r['avatar_path'] else url_for('static', filename='img/default-avatar.png') }}">
        <div>
          <div style="font-weight:700">{{ r['name'] }}</div>
          <div class="meta">關注於：{{ r['created_at'] }}</div>
          <div style="font-size:13px;color:#666">{{ r['email'] }}</div>
        </div>
      </div>
      <div class="actions">
        <a class="link" href="{{ url_for('user_public', uid=r['uid']) }}">查看</a>
        <form method="post" action="{{ url_for('remove_follower', uid=r['uid']) }}" onsubmit="return confirm('確定要移除此粉絲？');">
          <button class="btn" type="submit">移除粉絲</button>
        </form>
      </div>
    </div>
  {% endfor %}
{% else %}
  <p class="empty">目前還沒有粉絲。</p>
{% endif %}
''', rows=rows)

# === [ADD] 我追蹤中的列表 ===
@app.get('/profile/following')
@login_required
def my_following_page():
    conn = get_db()
    rows = conn.execute('''
        SELECT u.id, u.name, u.email, u.avatar_path
        FROM follows f
        JOIN users u ON u.id = f.followee_id
        WHERE f.follower_id=?
        ORDER BY f.created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()
    return render_template_string('''
<!doctype html><meta charset="utf-8"><title>我追蹤中</title>
<style>
body{font-family:Arial, sans-serif;max-width:760px;margin:24px auto;padding:0 16px;background:#f7f7f7}
h2{margin:6px 0 16px}
.item{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#fff;border:1px solid #eee;border-radius:10px;padding:12px 14px;margin-bottom:10px}
.left{display:flex;align-items:center;gap:12px}
img{width:44px;height:44px;border-radius:50%;object-fit:cover;background:#ddd}
a.link{color:#6b4aa1;text-decoration:none}
.back{display:inline-block;margin-bottom:12px}
.empty{color:#777}
</style>
<a class="back link" href="{{ url_for('profile') }}">← 回個人介面</a>
<h2>➡️ 我追蹤中</h2>
{% if rows %}
  {% for r in rows %}
    <div class="item">
      <div class="left">
        <img src="{{ url_for('static', filename=r['avatar_path']) if r['avatar_path'] else url_for('static', filename='img/default-avatar.png') }}">
        <div>
          <div style="font-weight:700">{{ r['name'] }}</div>
          <div style="font-size:13px;color:#666">{{ r['email'] }}</div>
        </div>
      </div>
      <a class="link" href="{{ url_for('user_public', uid=r['id']) }}">查看</a>
    </div>
  {% endfor %}
{% else %}
  <p class="empty">目前沒有追蹤任何人。</p>
{% endif %}
''', rows=rows)

# === [ADD] 真的移除粉絲（刪除對方→我的追蹤關係） ===
@app.post('/followers/remove/<int:uid>')
@login_required
def remove_follower(uid):
    conn = get_db()
    conn.execute('DELETE FROM follows WHERE follower_id=? AND followee_id=?',
                 (uid, session['user_id']))
    conn.commit()
    conn.close()
    flash('已移除粉絲')
    return redirect(url_for('my_followers_page'))

@app.route('/profile/update', methods=['POST'])
@login_required
def profile_update():
    """
    更新顯示名稱與自我介紹（bio）。
    - 同步更新 session['user_name']，讓右上角顯示立即生效
    """
    name = request.form.get('name','').strip()
    bio  = request.form.get('bio','').strip()
    if not name:
        flash('名稱不可空白')
        return redirect(url_for('profile'))
    conn = get_db()
    conn.execute('UPDATE users SET name=?, bio=? WHERE id=?',
                 (name, bio, session['user_id']))
    conn.commit()
    conn.close()
    session['user_name'] = name
    flash('個人資料已更新')
    return redirect(url_for('profile'))

@app.route('/profile/avatar', methods=['POST'])
@login_required
def profile_avatar():
    """
    上傳並更新頭像（配合前端 fetch，回傳 JSON）
    - 接受 jpg / png / webp
    - 檔案存至 static/avatars/<user_id>/avatar_<timestamp>_<rand>.<ext>
    - DB 存相對 static 的路徑：avatars/<user_id>/avatar_*.*
    - 回傳 { ok: True, url: "<靜態檔案URL>" }
    """
    f = request.files.get('avatar')
    if not f or f.filename == '':
        return jsonify({'ok': False, 'msg': '請選擇圖片'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        # 前端裁切預設已輸出 PNG，這裡保險改成 .png
        ext = '.png'

    user_id = session['user_id']
    save_dir = os.path.join('static', 'avatars', str(user_id))
    os.makedirs(save_dir, exist_ok=True)

    # 產生唯一檔名，並清理舊檔（避免資料夾累積）
    import time, secrets
    filename = f"avatar_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    save_path = os.path.join(save_dir, filename)

    # 刪除舊的 avatar_* 檔（可留 1~2 張也行，這裡全刪）
    try:
        for old in os.listdir(save_dir):
            if old.startswith('avatar_'):
                try:
                    os.remove(os.path.join(save_dir, old))
                except:
                    pass
    except FileNotFoundError:
        pass

    # 儲存新檔
    f.save(save_path)

    # 存 DB：相對 static 路徑（讓 url_for('static', filename=...) 可用）
    rel_path = f"avatars/{user_id}/{filename}"
    conn = get_db()
    conn.execute('UPDATE users SET avatar_path=? WHERE id=?', (rel_path, user_id))
    conn.commit()
    conn.close()

    # 回傳給前端使用的 URL（前端會再加時間戳破快取）
    url = url_for('static', filename=rel_path)
    return jsonify({'ok': True, 'url': url})

@app.route('/password', methods=['GET', 'POST'])
@login_required
def change_password():
    """
    修改密碼：
    - 檢查舊密碼是否正確
    - 新密碼與確認是否一致，且長度 >= 6
    成功後回到個人介面
    """
    if request.method == 'POST':
        old = request.form.get('old','')
        new = request.form.get('new','')
        confirm = request.form.get('confirm','')

        if not old or not new or not confirm:
            flash('請完整填寫')
            return render_template('password.html')
        if len(new) < 6:
            flash('新密碼至少 6 碼')
            return render_template('password.html')
        if new != confirm:
            flash('兩次新密碼不一致')
            return render_template('password.html')

        conn = get_db()
        user = conn.execute('SELECT password_hash FROM users WHERE id=?',
                            (session['user_id'],)).fetchone()
        if not user or not check_password_hash(user['password_hash'], old):
            conn.close()
            flash('舊密碼不正確')
            return render_template('password.html')

        conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                     (generate_password_hash(new), session['user_id']))
        conn.commit()
        conn.close()
        flash('密碼已更新')
        return redirect(url_for('profile'))

    # GET
    return render_template('password.html')

# -----------------------------
# 註冊 / 登入 / 登出
# -----------------------------
@app.route('/register', methods=['GET','POST'])
def register():
    # 若為 POST 請求，表示使用者送出註冊表單
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        # 驗證欄位是否完整、密碼長度是否足夠
        if not name or not email or not password or len(password) < 6:
            flash('請填寫完整，密碼至少 6 碼。')
            return render_template('register.html')
        # 寫入資料庫（建立新使用者）
        try:
            conn = get_db()
            conn.execute('INSERT INTO users (name, email, password_hash) VALUES (?,?,?)',
                         (name, email, generate_password_hash(password)))
            conn.commit()
            conn.close()
            flash('註冊成功，請登入。')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('此 Email 已被註冊。')
            return render_template('register.html')
    return render_template('register.html')

@app.route('/login', methods=['GET','POST'])
def login():
    # 若為 POST，代表使用者送出登入表單
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        conn.close()
        # 驗證帳號與密碼
        if user and check_password_hash(user['password_hash'], password):
            # 登入成功，建立 session
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            flash('登入成功！')
            return redirect(url_for('index'))
        # 登入失敗（帳密錯誤）
        flash('帳號或密碼錯誤。')
        return render_template('login.html')
    return render_template('login.html')

@app.route('/logout', methods=['POST','GET'])
def logout():
    # 清除 session 並返回登入頁
    session.clear()
    flash('您已登出。')
    return redirect(url_for('login'))

# -----------------------------
# 忘記密碼
# -----------------------------
@app.route('/forgot', methods=['GET','POST'])
def forgot():
    # 若為 POST 表示使用者送出 Email 以索取驗證碼
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        if not email:
            flash('請輸入 Email')
            return render_template('forgot.html')

        conn = get_db()
        user = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if not user:
            conn.close()
            flash('此 Email 未註冊。')
            return render_template('forgot.html')

        # 限制發信頻率（每 60 秒可再發一次）
        last = conn.execute('SELECT created_at FROM password_resets WHERE user_id=? ORDER BY id DESC LIMIT 1',
                            (user['id'],)).fetchone()
        if last:
            try:
                last_dt = datetime.strptime(last['created_at'], '%Y-%m-%d %H:%M:%S')
                if (datetime.now() - last_dt).total_seconds() < 60:
                    conn.close()
                    flash('請稍候再索取驗證碼（每 60 秒可重送一次）。')
                    return render_template('forgot.html')
            except:
                pass

        # 產生 6 位數驗證碼並設定 10 分鐘有效期
        code = gen_6_code()
        expires_at = (datetime.now() + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('INSERT INTO password_resets (user_id, code, expires_at) VALUES (?,?,?)',
                     (user['id'], code, expires_at))
        conn.commit()
        conn.close()

      # 嘗試寄信（若 SMTP 未設定，則會顯示於cmd）
        try:
            send_code_email(email, code)
            flash('驗證碼已寄到您的信箱，10 分鐘內有效。請至下方頁面輸入驗證碼與新密碼。')
        except Exception as e:
            print("寄信失敗：", e)
            flash('寄信失敗，但已產生驗證碼（本機開發模式請看主控台輸出）。')

        return render_template('reset.html', email=email)

    return render_template('forgot.html')

@app.route('/reset', methods=['POST'])
def reset_password():
    # 取得使用者輸入資料
    email = request.form.get('email','').strip().lower()
    code = request.form.get('code','').strip()
    new_password = request.form.get('password','')
    confirm = request.form.get('confirm','')

    # 檢查輸入完整性
    if not email or not code or not new_password:
        flash('請完整輸入 Email、驗證碼與新密碼。')
        return render_template('reset.html', email=email)

    if new_password != confirm:
        flash('兩次密碼不一致。')
        return render_template('reset.html', email=email)

    if len(new_password) < 6:
        flash('新密碼至少 6 碼。')
        return render_template('reset.html', email=email)

    conn = get_db()
    user = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if not user:
        conn.close()
        flash('此 Email 未註冊。')
        return render_template('reset.html', email=email)

    # 取出最後一筆驗證碼
    row = conn.execute('''
        SELECT code, expires_at FROM password_resets
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 1
    ''', (user['id'],)).fetchone()

    if not row:
        conn.close()
        flash('請先索取驗證碼。')
        return render_template('reset.html', email=email)

    now = datetime.now()
    exp = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
    if row['code'] != code or now > exp:
        conn.close()
        flash('驗證碼錯誤或已過期。')
        return render_template('reset.html', email=email)

    # 更新密碼（覆寫 hash）
    conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                 (generate_password_hash(new_password), user['id']))
    conn.commit()
    conn.close()
    flash('密碼重設成功！請登入。')
    return redirect(url_for('login'))

# -------- 查看單筆我的結果（修正 404） --------
@app.route('/history/view/<int:rid>')
@login_required
def view_history(rid):
    # 查詢單筆歷史紀錄資料（必須屬於目前登入者）
    conn = get_db()
    row = conn.execute('''
        SELECT original_filename, input_image, detected_image, counts_json, objects_text
        FROM results
        WHERE id=? AND user_id=?
    ''', (rid, session['user_id'])).fetchone()
    conn.close()
    # 若查無資料 → 回傳 404 錯誤
    if not row:
        return "找不到此歷史紀錄", 404
   # 將 counts_json 轉為 Python dict（統計資料）
    counts = json.loads(row['counts_json']) if row['counts_json'] else {}
    return render_template('result.html',
                           username=session.get('user_name'),
                           input_image=url_for('static', filename=row['input_image']) if row['input_image'] else None,
                           detected_image=url_for('static', filename=row['detected_image']) if row['detected_image'] else None,
                           objects_text=row['objects_text'],
                           counts_json=json.dumps(counts, ensure_ascii=False),
                           original_filename=row['original_filename'],
                           show_original=bool(row['input_image']),
                           show_circle=bool(row['detected_image'])
                           )

# -------- 上傳頭像（裁切後的圓形 PNG） --------
@app.route('/profile/avatar', methods=['POST'])
@login_required
def upload_avatar():
    file = request.files.get('avatar')
    if not file:
        # 明確回 JSON，避免 302
        return jsonify({'ok': False, 'msg': '缺少檔案'}), 400

    user_id = session['user_id']
    save_dir = os.path.join('static', 'avatars', str(user_id))
    os.makedirs(save_dir, exist_ok=True)

    filename = f"avatar_{int(time.time())}.png"
    save_path = os.path.join(save_dir, filename)
    file.save(save_path)

    # （可選）更新 DB 中使用者的 avatar_path 欄位；沒有此欄位可整段 try: … except: pass
    try:
        conn = get_db()
        rel_path = os.path.join('avatars', str(user_id), filename)  # 相對 static 的路徑
        conn.execute('UPDATE users SET avatar_path=? WHERE id=?', (rel_path, user_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

    url = url_for('static', filename=os.path.join('avatars', str(user_id), filename))
    # 關鍵：回 200 JSON，不要 redirect
    return jsonify({'ok': True, 'url': url}), 200

    
# === [ADD] 送出留言 ===
@app.route('/post/<int:post_id>/comment', methods=['POST'])
@login_required
def post_comment(post_id):
    content = (request.form.get('content') or '').strip()
    if content:
        conn = get_db()
        conn.execute(
            'INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
            (post_id, session['user_id'], content)
        )
        conn.commit()
        conn.close()
    return redirect(url_for('feed') + f'#post-{post_id}')

# === [ADD] 按讚 / 取消讚（切換） ===
@app.route('/post/<int:post_id>/like', methods=['POST'])
@login_required
def toggle_like(post_id):
    user_id = session['user_id']
    conn = get_db()
    cur = conn.cursor()
    # 嘗試插入（按讚）；若已存在則改成刪除（取消讚）
    try:
        cur.execute('INSERT INTO likes (user_id, post_id) VALUES (?, ?)', (user_id, post_id))
        conn.commit()
        liked = True
    except Exception:
        cur.execute('DELETE FROM likes WHERE user_id=? AND post_id=?', (user_id, post_id))
        conn.commit()
        liked = False
    # 回傳最新讚數
    like_count = cur.execute('SELECT COUNT(*) FROM likes WHERE post_id=?', (post_id,)).fetchone()[0]
    conn.close()
    return {'ok': True, 'liked': liked, 'count': like_count}

# =============================
# 社群：貼文牆（大家都看得到）
# =============================
@app.route('/feed', methods=['GET', 'POST'])
@login_required
def feed():
    # 發文（文字 + 可選圖片）
    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        img = request.files.get('image')
        if not content and (not img or img.filename == ''):
            flash('請輸入內容或上傳圖片')
            return redirect(url_for('feed'))
        image_path = None
        if img and img.filename:
            image_path = save_image_to_static(img, 'post_images')

        conn = get_db()
        conn.execute('INSERT INTO posts (user_id, content, image_path) VALUES (?,?,?)',
                     (session['user_id'], content, image_path))
        conn.commit()
        conn.close()
        return redirect(url_for('feed'))

    # 讀取貼文（含作者名、頭貼、按讚數、留言數）
    conn = get_db()
    rows = conn.execute('''
        SELECT p.id, p.user_id, p.content, p.image_path, p.created_at,
               u.name AS author_name, u.avatar_path
        FROM posts p
        JOIN users u ON u.id = p.user_id
        ORDER BY p.id DESC
        LIMIT 200
    ''').fetchall()

    posts = []
    for r in rows:
        # 計數
        like_count = conn.execute('SELECT COUNT(*) AS c FROM likes WHERE post_id=?', (r['id'],)).fetchone()['c']
        comment_count = conn.execute('SELECT COUNT(*) AS c FROM comments WHERE post_id=?', (r['id'],)).fetchone()['c']
        # 我是否按過讚
        me_liked = conn.execute('SELECT 1 FROM likes WHERE post_id=? AND user_id=?',
                                (r['id'], session['user_id'])).fetchone() is not None
        posts.append({
            "id": r['id'],
            "user_id": r['user_id'],
            "content": r['content'],
            "image_url": (url_for('static', filename=r['image_path']) if r['image_path'] else None),
            "created_at": r['created_at'],
            "author_name": r['author_name'],
            "author_avatar": url_for('static', filename=r['avatar_path']) if r['avatar_path'] else url_for('static', filename='img/default-avatar.png'),
            "like_count": like_count,
            "comment_count": comment_count,
            "me_liked": me_liked
        })
    conn.close()
    return render_template('feed.html', posts=posts)

# =============================
# 單篇貼文詳情（誰按讚、所有留言）
# =============================
@app.route('/post/<int:pid>', methods=['GET', 'POST'])
@login_required
def post_detail(pid):
    conn = get_db()

    # 新增留言
    if request.method == 'POST':
        content = request.form.get('content','').strip()
        if content:
            conn.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?,?,?)',
                         (pid, session['user_id'], content))
            conn.commit()
        return redirect(url_for('post_detail', pid=pid))

    # 讀貼文
    p = conn.execute('''
        SELECT p.id, p.user_id, p.content, p.image_path, p.created_at,
               u.name AS author_name, u.avatar_path
        FROM posts p
        JOIN users u ON u.id = p.user_id
        WHERE p.id=?
    ''', (pid,)).fetchone()
    if not p:
        conn.close()
        return "貼文不存在", 404

    # 讚的人
    likers = conn.execute('''
        SELECT l.user_id, u.name, u.avatar_path
        FROM likes l
        JOIN users u ON u.id = l.user_id
        WHERE l.post_id=?
        ORDER BY l.created_at DESC
    ''', (pid,)).fetchall()

    # 留言（含頭貼與名字）
    comments = conn.execute('''
        SELECT c.id, c.content, c.created_at, c.user_id,
               u.name, u.avatar_path
        FROM comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.post_id=?
        ORDER BY c.id ASC
    ''', (pid,)).fetchall()

    me_liked = conn.execute('SELECT 1 FROM likes WHERE post_id=? AND user_id=?',
                            (pid, session['user_id'])).fetchone() is not None

    post = {
        "id": p['id'],
        "user_id": p['user_id'],
        "content": p['content'],
        "image_url": (url_for('static', filename=p['image_path']) if p['image_path'] else None),
        "created_at": p['created_at'],
        "author_name": p['author_name'],
        "author_avatar": url_for('static', filename=p['avatar_path']) if p['avatar_path'] else url_for('static', filename='img/default-avatar.png'),
        "me_liked": me_liked
    }
    liker_list = [{
        "user_id": r['user_id'],
        "name": r['name'],
        "avatar": url_for('static', filename=r['avatar_path']) if r['avatar_path'] else url_for('static', filename='img/default-avatar.png')
    } for r in likers]
    comment_list = [{
        "id": c['id'],
        "user_id": c['user_id'],
        "name": c['name'],
        "avatar": url_for('static', filename=c['avatar_path']) if c['avatar_path'] else url_for('static', filename='img/default-avatar.png'),
        "content": c['content'],
        "created_at": c['created_at']
    } for c in comments]

    conn.close()
    return render_template('post_detail.html', post=post, likers=liker_list, comments=comment_list)

# =============================
# 按讚/收回讚（切換）
# =============================
@app.route('/like/<int:pid>', methods=['POST'])
@login_required
def like_toggle(pid):
    conn = get_db()
    has = conn.execute('SELECT 1 FROM likes WHERE post_id=? AND user_id=?',
                       (pid, session['user_id'])).fetchone()
    if has:
        conn.execute('DELETE FROM likes WHERE post_id=? AND user_id=?', (pid, session['user_id']))
    else:
        conn.execute('INSERT OR IGNORE INTO likes (post_id, user_id) VALUES (?,?)',
                     (pid, session['user_id']))
    conn.commit()
    conn.close()
    # 來源在哪就回哪
    refer = request.headers.get('Referer') or url_for('feed')
    return redirect(refer)

# =============================
# 我按過的讚（列表）
# =============================
@app.route('/likes')
@login_required
def my_likes():
    conn = get_db()
    rows = conn.execute('''
        SELECT p.id, p.content, p.image_path, p.created_at,
               u.name AS author_name, u.avatar_path
        FROM likes l
        JOIN posts p ON p.id = l.post_id
        JOIN users u ON u.id = p.user_id
        WHERE l.user_id=?
        ORDER BY l.created_at DESC
        LIMIT 200
    ''', (session['user_id'],)).fetchall()
    items = [{
        "post_id": r['id'],
        "content": r['content'],
        "image_url": (url_for('static', filename=r['image_path']) if r['image_path'] else None),
        "author_name": r['author_name'],
        "author_avatar": url_for('static', filename=r['avatar_path']) if r['avatar_path'] else url_for('static', filename='img/default-avatar.png'),
        "created_at": r['created_at']
    } for r in rows]
    conn.close()
    return render_template('likes.html', items=items)

# =============================
# 公開個人頁（可被點頭像跳轉）
# =============================
@app.route('/u/<int:uid>')
@login_required
def user_public(uid):
    conn = get_db()
    user = conn.execute('SELECT id, name, email, created_at, bio, avatar_path FROM users WHERE id=?', (uid,)).fetchone()
    if not user:
        conn.close()
        return "使用者不存在", 404

    # 統計
    follower_cnt = conn.execute('SELECT COUNT(*) AS c FROM follows WHERE followee_id=?', (uid,)).fetchone()['c']
    following_cnt = conn.execute('SELECT COUNT(*) AS c FROM follows WHERE follower_id=?', (uid,)).fetchone()['c']
    is_me = (uid == session['user_id'])
    is_following = False
    if not is_me:
        is_following = conn.execute('SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?',
                                    (session['user_id'], uid)).fetchone() is not None

    # 該使用者的貼文
    posts = conn.execute('''
        SELECT id, content, image_path, created_at
        FROM posts
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 100
    ''', (uid,)).fetchall()
    conn.close()

    avatar = url_for('static', filename=user['avatar_path']) if user['avatar_path'] else url_for('static', filename='img/default-avatar.png')
    return render_template('user_public.html',
                           user={
                               "id": user['id'],
                               "name": user['name'],
                               "bio": user['bio'],
                               "avatar": avatar,
                               "created_at": user['created_at'],
                               "follower_cnt": follower_cnt,
                               "following_cnt": following_cnt,
                               "is_me": is_me,
                               "is_following": is_following
                           },
                           posts=[{
                               "id": p['id'],
                               "content": p['content'],
                               "image_url": (url_for('static', filename=p['image_path']) if p['image_path'] else None),
                               "created_at": p['created_at']
                           } for p in posts])

# 追蹤/取消追蹤（POST）
@app.route('/follow/<int:uid>', methods=['POST'])
@login_required
def follow_user(uid):
    if uid == session['user_id']:
        return redirect(url_for('user_public', uid=uid))
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO follows (follower_id, followee_id) VALUES (?,?)',
                 (session['user_id'], uid))
    conn.commit()
    conn.close()
    return redirect(url_for('user_public', uid=uid))

@app.route('/unfollow/<int:uid>', methods=['POST'])
@login_required
def unfollow_user(uid):
    conn = get_db()
    conn.execute('DELETE FROM follows WHERE follower_id=? AND followee_id=?',
                 (session['user_id'], uid))
    conn.commit()
    conn.close()
    return redirect(url_for('user_public', uid=uid))

# =========================
# ✅ 新增：查看「別人的」粉絲
# =========================
@app.get('/u/<int:uid>/followers')
@login_required
def user_followers(uid):
    conn = get_db()
    target = conn.execute('SELECT id, name FROM users WHERE id=?', (uid,)).fetchone()
    if not target:
        conn.close()
        return "使用者不存在", 404
    rows = conn.execute('''
        SELECT u.id, u.name, u.email, u.avatar_path, f.created_at
        FROM follows f
        JOIN users u ON u.id = f.follower_id
        WHERE f.followee_id=?
        ORDER BY f.created_at DESC
    ''', (uid,)).fetchall()
    conn.close()
    return render_template_string('''
<!doctype html><meta charset="utf-8"><title>{{ target_name }} 的粉絲</title>
<style>body{font-family:Arial;margin:24px auto;max-width:800px;padding:0 16px;background:#f7f7f7}
.item{display:flex;justify-content:space-between;align-items:center;background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px}
.left{display:flex;align-items:center;gap:10px}
img{width:48px;height:48px;border-radius:50%;object-fit:cover}
a{color:#6b4aa1;text-decoration:none}
.meta{color:#666;font-size:13px}
</style>
<h2>👥 {{ target_name }} 的粉絲 ({{ rows|length }})</h2>
<a href="{{ url_for('user_public', uid=uid) }}">← 回 {{ target_name }} 的頁面</a>
{% for r in rows %}
<div class="item">
  <div class="left">
    <img src="{{ url_for('static', filename=r['avatar_path']) if r['avatar_path'] else url_for('static', filename='img/default-avatar.png') }}">
    <div>
      <b>{{ r['name'] }}</b><br><span class="meta">{{ r['email'] }}</span>
    </div>
  </div>
  <a href="{{ url_for('user_public', uid=r['id']) }}">查看</a>
</div>
{% else %}
<p>暫無粉絲。</p>
{% endfor %}
''', rows=rows, uid=uid, target_name=target['name'])

# =========================
# ✅ 新增：查看「別人」追蹤中
# =========================
@app.get('/u/<int:uid>/following')
@login_required
def user_following(uid):
    conn = get_db()
    target = conn.execute('SELECT id, name FROM users WHERE id=?', (uid,)).fetchone()
    if not target:
        conn.close()
        return "使用者不存在", 404
    rows = conn.execute('''
        SELECT u.id, u.name, u.email, u.avatar_path, f.created_at
        FROM follows f
        JOIN users u ON u.id = f.followee_id
        WHERE f.follower_id=?
        ORDER BY f.created_at DESC
    ''', (uid,)).fetchall()
    conn.close()
    return render_template_string('''
<!doctype html><meta charset="utf-8"><title>{{ target_name }} 的追蹤中</title>
<style>body{font-family:Arial;margin:24px auto;max-width:800px;padding:0 16px;background:#f7f7f7}
.item{display:flex;justify-content:space-between;align-items:center;background:#fff;border-radius:10px;padding:10px 14px;margin-bottom:10px}
.left{display:flex;align-items:center;gap:10px}
img{width:48px;height:48px;border-radius:50%;object-fit:cover}
a{color:#6b4aa1;text-decoration:none}
.meta{color:#666;font-size:13px}
</style>
<h2>➡️ {{ target_name }} 追蹤中 ({{ rows|length }})</h2>
<a href="{{ url_for('user_public', uid=uid) }}">← 回 {{ target_name }} 的頁面</a>
{% for r in rows %}
<div class="item">
  <div class="left">
    <img src="{{ url_for('static', filename=r['avatar_path']) if r['avatar_path'] else url_for('static', filename='img/default-avatar.png') }}">
    <div>
      <b>{{ r['name'] }}</b><br><span class="meta">{{ r['email'] }}</span>
    </div>
  </div>
  <a href="{{ url_for('user_public', uid=r['id']) }}">查看</a>
</div>
{% else %}
<p>暫無追蹤。</p>
{% endfor %}
''', rows=rows, uid=uid, target_name=target['name'])

# ============== 教學清單（宮格） ==============
@app.route('/tutorials')
def tutorials():
    tutorials_root = os.path.join('static', 'tutorials')
    items = []

    if os.path.isdir(tutorials_root):
        for fname in sorted(os.listdir(tutorials_root)):
            base, ext = os.path.splitext(fname)
            low_ext = ext.lower()
            if (
                low_ext in ALLOWED_IMG_EXTS
                and base in name_mapping
                and os.path.isfile(os.path.join(tutorials_root, fname))
            ):
                items.append({
                    "key": base,
                    "title": name_mapping[base],
                    "thumb_url": url_for('static', filename=f"tutorials/{fname}")
                })

    # 只顯示實際存在且在 name_mapping 裡的圖片
    return render_template('tutorials.html', items=items)



# ============== 教學詳情（整張圖片） ==============
@app.route('/tutorials/<key>')
def tutorial_detail(key):
    title = name_mapping.get(key, key)  # 找不到對照就用 key
    tutorials_root = os.path.join('static', 'tutorials')

    # 允許多種副檔名，找到第一張就用
    image_url = None
    for ext in ALLOWED_IMG_EXTS:
        fpath = os.path.join(tutorials_root, key + ext)
        if os.path.isfile(fpath):
            image_url = url_for('static', filename=f"tutorials/{key}{ext}")
            break

    if not image_url:
        # 沒有對應檔案就顯示溫和訊息
        return render_template('tutorial_detail.html',
                               title=title, key=key, image_url=None)

    return render_template('tutorial_detail.html',
                           title=title, key=key, image_url=image_url)



# -----------------------------
# 啟動
# -----------------------------
if __name__ == '__main__':
    # 初始化資料庫（若尚未建立會自動建表）
    init_db()
    # 確保必要的靜態資料夾存在    
    os.makedirs('static/uploads', exist_ok=True)
    os.makedirs('static/results', exist_ok=True)
    os.makedirs('static/avatars', exist_ok=True)
    # 啟動 Flask 伺服器（debug 模式）
    app.run(host='0.0.0.0', port=5000, debug=True)
