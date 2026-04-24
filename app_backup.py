import os
import json
import sqlite3
import traceback
from uuid import uuid4
from contextlib import closing
from datetime import datetime, timezone

import stripe
from mutagen import File as MutagenFile
from dotenv import load_dotenv
from openai import OpenAI
from passlib.context import CryptContext
from fastapi import FastAPI, HTTPException, Request, Query, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

load_dotenv()

app = FastAPI()

# =========================
# 設定
# =========================
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")

stripe.api_key = STRIPE_SECRET_KEY
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

print("STRIPE_SECRET_KEY loaded =", bool(STRIPE_SECRET_KEY))
print("STRIPE_WEBHOOK_SECRET loaded =", STRIPE_WEBHOOK_SECRET)
print("OPENAI_API_KEY loaded =", bool(OPENAI_API_KEY))
print("ADMIN_EMAIL loaded =", ADMIN_EMAIL)

PRICE_MAP = {
    "standard": "price_1TP6bjRymXQ1NPXw1JfHdxMZ",
    "pro": "price_1TP6dARymXQ1NPXwPivUepu8",
    "business": "price_1TP6dTRymXQ1NPXwd8KYb1D9",
}

PLAN_LIMITS_SECONDS = {
    "free": 10 * 60,
    "standard": 120 * 60,
    "pro": 600 * 60,
    "business": 100000 * 60,
}

PUBLIC_MAX_SECONDS = 60

DB_PATH = "app.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


# =========================
# DB
# =========================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column_exists(table_name: str, column_name: str, column_type_sql: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table_name})")
        cols = [row["name"] for row in cur.fetchall()]
        if column_name not in cols:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type_sql}")
            conn.commit()


def init_db():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            email TEXT UNIQUE,
            password_hash TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            plan TEXT DEFAULT 'free',
            status TEXT DEFAULT 'inactive',
            current_period_end TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS stripe_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE,
            event_type TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT UNIQUE,
            user_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS transcriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT,
            original_filename TEXT,
            duration_seconds INTEGER DEFAULT 0,
            raw_text TEXT,
            cleaned_text TEXT,
            transform_type TEXT,
            transformed_text TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS transform_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcription_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            transform_type TEXT NOT NULL,
            transformed_text TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.commit()

    ensure_column_exists("users", "username", "TEXT")
    ensure_column_exists("users", "password_hash", "TEXT")
    ensure_column_exists("transcriptions", "raw_text", "TEXT")
    ensure_column_exists("transcriptions", "cleaned_text", "TEXT")
    ensure_column_exists("transcriptions", "transform_type", "TEXT")
    ensure_column_exists("transcriptions", "transformed_text", "TEXT")


@app.on_event("startup")
def startup():
    init_db()


# =========================
# 共通
# =========================
def now_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def meta_value(metadata, key: str):
    if metadata is None:
        return None
    try:
        return metadata[key]
    except Exception:
        pass
    try:
        return getattr(metadata, key, None)
    except Exception:
        return None


def is_admin(user: dict | None) -> bool:
    if not user or not ADMIN_EMAIL:
        return False
    return (user.get("email") or "").lower() == ADMIN_EMAIL.lower()


def render_page(title: str, body: str) -> str:
    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>{title}</title>
        <style>
            body {{
                font-family: Arial, "Yu Gothic UI", sans-serif;
                max-width: 1100px;
                margin: 40px auto;
                padding: 0 20px;
                background: #fff8f3;
                color: #3c2a21;
            }}
            .card {{
                background: #fffdfb;
                border-radius: 16px;
                padding: 24px;
                box-shadow: 0 8px 24px rgba(120,72,32,0.10);
                margin-bottom: 20px;
                border: 1px solid #f3d9c5;
            }}
            .btn {{
                display: inline-block;
                padding: 12px 18px;
                margin: 6px 8px 6px 0;
                border-radius: 10px;
                text-decoration: none;
                background: #d97706;
                color: white;
                font-weight: bold;
                border: none;
                cursor: pointer;
            }}
            .btn-sub {{
                background: #9a6b4f;
            }}
            .btn-green {{
                background: #b45309;
            }}
            .btn-red {{
                background: #c2410c;
            }}
            .muted {{
                color: #8b6b5c;
            }}
            pre {{
                background: #4a2f27;
                color: #fff7ed;
                padding: 16px;
                border-radius: 12px;
                overflow-x: auto;
                white-space: pre-wrap;
                word-break: break-word;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 12px;
                overflow: hidden;
            }}
            th, td {{
                padding: 12px;
                border-bottom: 1px solid #f1e2d8;
                text-align: left;
                vertical-align: top;
            }}
            th {{
                background: #fff1e6;
            }}
            .pill {{
                display: inline-block;
                padding: 4px 10px;
                border-radius: 999px;
                background: #fde7d7;
                color: #9a3412;
                font-size: 12px;
                font-weight: bold;
            }}
            input[type=text], input[type=password], input[type=email], input[type=file], input[type=number], select {{
                width: 100%;
                max-width: 560px;
                padding: 12px;
                border: 1px solid #e7cbb7;
                border-radius: 10px;
                margin: 6px 0 14px;
                background: #fffaf7;
            }}
            label {{
                display: block;
                font-weight: bold;
                margin-top: 8px;
            }}
        </style>
    </head>
    <body>
        {body}
    </body>
    </html>
    """


# =========================
# 認証
# =========================
def validate_password(password: str):
    if len(password) < 8:
        raise ValueError("パスワードは8文字以上にしてください")
    if len(password) > 128:
        raise ValueError("パスワードは128文字以内にしてください")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    return pwd_context.verify(password, password_hash)


def create_user(username: str, email: str, password: str):
    validate_password(password)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        if cur.fetchone():
            raise ValueError("そのメールアドレスは既に登録されています")

        cur.execute("""
            INSERT INTO users (username, email, password_hash, plan, status)
            VALUES (?, ?, ?, 'free', 'inactive')
        """, (username, email, hash_password(password)))
        conn.commit()


def get_user_by_email(email: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        return dict(row) if row else None


def create_session(user_id: int) -> str:
    token = str(uuid4())
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sessions (session_token, user_id) VALUES (?, ?)",
            (token, user_id),
        )
        conn.commit()
    return token


def delete_session(token: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE session_token = ?", (token,))
        conn.commit()


def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT users.*
            FROM sessions
            JOIN users ON sessions.user_id = users.id
            WHERE sessions.session_token = ?
        """, (token,))
        row = cur.fetchone()
        return dict(row) if row else None


# =========================
# 利用量 / 履歴
# =========================
def get_monthly_used_seconds(user_id: int) -> int:
    month_prefix = now_month_key()
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(duration_seconds), 0) AS total
            FROM transcriptions
            WHERE user_id = ?
              AND strftime('%Y-%m', created_at) = ?
        """, (user_id, month_prefix))
        row = cur.fetchone()
        return int(row["total"]) if row and row["total"] is not None else 0


def get_user_limit_seconds(user: dict) -> int:
    plan = user.get("plan") or "free"
    return PLAN_LIMITS_SECONDS.get(plan, PLAN_LIMITS_SECONDS["free"])


def save_transcription(
    user_id: int,
    filename: str,
    original_filename: str,
    duration_seconds: int,
    raw_text: str,
    cleaned_text: str,
    transform_type: str | None = None,
    transformed_text: str | None = None,
):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO transcriptions (
                user_id, filename, original_filename, duration_seconds,
                raw_text, cleaned_text, transform_type, transformed_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            filename,
            original_filename,
            duration_seconds,
            raw_text,
            cleaned_text,
            transform_type,
            transformed_text,
        ))
        conn.commit()
        return cur.lastrowid


def update_transcription_transform(record_id: int, transform_type: str, transformed_text: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE transcriptions
            SET transform_type = ?, transformed_text = ?
            WHERE id = ?
        """, (transform_type, transformed_text, record_id))
        conn.commit()


def save_transform_history(transcription_id: int, user_id: int, transform_type: str, transformed_text: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO transform_history (transcription_id, user_id, transform_type, transformed_text)
            VALUES (?, ?, ?, ?)
        """, (transcription_id, user_id, transform_type, transformed_text))
        conn.commit()


def get_transform_history(transcription_id: int, user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, transcription_id, user_id, transform_type, transformed_text, created_at
            FROM transform_history
            WHERE transcription_id = ? AND user_id = ?
            ORDER BY id DESC
        """, (transcription_id, user_id))
        return [dict(row) for row in cur.fetchall()]


def get_user_transcriptions(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, filename, original_filename, duration_seconds,
                   raw_text, cleaned_text, transform_type, transformed_text, created_at
            FROM transcriptions
            WHERE user_id = ?
            ORDER BY id DESC
        """, (user_id,))
        return [dict(row) for row in cur.fetchall()]


def get_transcription_by_id(user_id: int, transcription_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, filename, original_filename, duration_seconds,
                   raw_text, cleaned_text, transform_type, transformed_text, created_at
            FROM transcriptions
            WHERE id = ? AND user_id = ?
        """, (transcription_id, user_id))
        row = cur.fetchone()
        return dict(row) if row else None


# =========================
# Stripe DB
# =========================
def event_already_processed(event_id: str) -> bool:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM stripe_events WHERE event_id = ?", (event_id,))
        return cur.fetchone() is not None


def mark_event_processed(event_id: str, event_type: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO stripe_events (event_id, event_type) VALUES (?, ?)",
            (event_id, event_type),
        )
        conn.commit()


def upsert_user_subscription(
    username: str | None,
    email: str | None,
    stripe_customer_id: str | None,
    stripe_subscription_id: str | None,
    plan: str | None,
    status: str | None,
    current_period_end: str | None = None,
):
    if not email:
        return

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = cur.fetchone()

        if row:
            cur.execute("""
                UPDATE users
                SET username = COALESCE(?, username),
                    stripe_customer_id = COALESCE(?, stripe_customer_id),
                    stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                    plan = COALESCE(?, plan),
                    status = COALESCE(?, status),
                    current_period_end = COALESCE(?, current_period_end),
                    updated_at = CURRENT_TIMESTAMP
                WHERE email = ?
            """, (
                username,
                stripe_customer_id,
                stripe_subscription_id,
                plan,
                status,
                current_period_end,
                email,
            ))
        else:
            cur.execute("""
                INSERT INTO users (
                    username, email, stripe_customer_id, stripe_subscription_id,
                    plan, status, current_period_end
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                username,
                email,
                stripe_customer_id,
                stripe_subscription_id,
                plan,
                status,
                current_period_end,
            ))

        conn.commit()


def update_user_by_customer(
    stripe_customer_id: str | None,
    *,
    plan: str | None = None,
    status: str | None = None,
    stripe_subscription_id: str | None = None,
    current_period_end: str | None = None,
):
    if not stripe_customer_id:
        return

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE users
            SET plan = COALESCE(?, plan),
                status = COALESCE(?, status),
                stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                current_period_end = COALESCE(?, current_period_end),
                updated_at = CURRENT_TIMESTAMP
            WHERE stripe_customer_id = ?
        """, (
            plan,
            status,
            stripe_subscription_id,
            current_period_end,
            stripe_customer_id,
        ))
        conn.commit()


def get_all_users():
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, username, email, stripe_customer_id, stripe_subscription_id,
                   plan, status, current_period_end, created_at, updated_at
            FROM users
            ORDER BY id DESC
        """)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


# =========================
# AI処理
# =========================
def transcribe_file_with_openai(file_path: str) -> str:
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY が未設定です")

    with open(file_path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f,
        )

    return getattr(result, "text", "") or ""


def clean_transcript_text(raw_text: str) -> str:
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY が未設定です")

    prompt = f"""
以下は音声文字起こしの生テキストです。
意味を変えずに、次のルールで整形してください。

ルール:
- 「えー」「あのー」「そのー」など不要なフィラーを削除
- 明らかな言い直しや重複を自然に整理
- 句読点を適切に付ける
- 読みやすい自然な日本語の文章にする
- 内容を勝手に追加しない
- 箇条書きではなく通常の文章で返す

文字起こし:
{raw_text}
""".strip()

    response = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )
    return response.output_text.strip()


def transform_text(cleaned_text: str, transform_type: str, plan: str) -> str:
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY が未設定です")

    if plan == "free":
        raise ValueError("変換機能は有料プラン限定です")

    prompt_map = {
        "x_posts": f"""
以下の文章をもとに、X投稿用の短文を3案作成してください。
条件:
- 各案140文字以内
- 日本語
- 読みやすく自然な文
- 3案を番号付きで出力

元文章:
{cleaned_text}
""",
        "x_thread": f"""
以下の文章をもとに、Xのスレッド形式で投稿案を作成してください。
条件:
- 最初に強いフック
- 5投稿前後
- 読みたくなる流れ
- 日本語で自然に

元文章:
{cleaned_text}
""",
        "short_30": f"""
以下の文章をもとに、YouTubeショート30秒用の台本を作成してください。
条件:
- 冒頭にフック
- 話し言葉
- 短くテンポ良く
- 最後に軽い締め

元文章:
{cleaned_text}
""",
        "short_60": f"""
以下の文章をもとに、YouTubeショート60秒用の台本を作成してください。
条件:
- 冒頭にフック
- 展開がわかりやすい
- 話し言葉
- 最後まで見たくなる構成

元文章:
{cleaned_text}
""",
        "blog": f"""
以下の文章をもとに、ブログ記事を作成してください。
条件:
- 見出し構成あり
- 導入とまとめあり
- 1000〜3000文字
- 読みやすく自然な日本語

元文章:
{cleaned_text}
""",
        "summary_3": f"""
以下の文章を3行で要約してください。

元文章:
{cleaned_text}
""",
        "summary_1min": f"""
以下の文章を1分で読める長さで要約してください。
条件:
- 重要点を落とさない
- 読みやすい自然な日本語

元文章:
{cleaned_text}
""",
    }

    if transform_type not in prompt_map:
        raise ValueError("不正な変換タイプです")

    response = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=prompt_map[transform_type],
    )
    return response.output_text.strip()


def add_free_watermark(text: str) -> str:
    return text + "\n\n---\n無料版で生成されました"


def save_upload(upload: UploadFile) -> tuple[str, str]:
    safe_name = f"{uuid4()}_{upload.filename}"
    save_path = os.path.join(UPLOAD_DIR, safe_name)
    return safe_name, save_path


def detect_audio_duration_seconds(file_path: str) -> int:
    audio = MutagenFile(file_path)
    if audio is None or not hasattr(audio, "info") or not hasattr(audio.info, "length"):
        raise ValueError("音声の長さを取得できませんでした")
    return max(1, int(round(audio.info.length)))


# =========================
# ページ
# =========================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)

    auth_block = ""
    if user:
        used = get_monthly_used_seconds(user["id"])
        limit_sec = get_user_limit_seconds(user)
        auth_block = f"""
        <div class="card">
            <h2>ログイン中</h2>
            <p><strong>{user.get("username") or user.get("email")}</strong></p>
            <p>プラン: <span class="pill">{user.get("plan") or "free"}</span></p>
            <p>状態: <span class="pill">{user.get("status") or "-"}</span></p>
            <p>今月使用量: <strong>{used} 秒</strong> / {limit_sec} 秒</p>

            <a class="btn" href="/dashboard">履歴とマイページ</a>
            <a class="btn btn-green" href="/portal">Customer Portal</a>
            {"<a class='btn btn-sub' href='/users'>users</a>" if is_admin(user) else ""}
            <a class="btn btn-sub" href="/logout">ログアウト</a>
        </div>
        """
    else:
        auth_block = """
        <div class="card">
            <h2>会員機能</h2>
            <p>履歴保存・再生成・プラン購入・Customer Portal はログインが必要です。</p>
            <a class="btn" href="/register">新規登録</a>
            <a class="btn btn-sub" href="/login">ログイン</a>
        </div>
        """

    body = f"""
    <div class="card">
        <h1>音声文字起こしAI</h1>
        <p>ログインしなくても文字起こしは使えます。</p>
        <p class="muted">未ログイン利用は履歴保存なし・1回 {PUBLIC_MAX_SECONDS} 秒まで・変換機能なしです。</p>
    </div>

    <div class="card">
        <h2>ログイン不要の文字起こし</h2>
        <form method="post" action="/transcribe-public" enctype="multipart/form-data">
            <label>音声ファイル</label>
            <input type="file" name="audio_file" required>
            <button class="btn" type="submit">文字起こしする</button>
        </form>
    </div>

    {auth_block}
    """
    return HTMLResponse(render_page("トップ", body))


@app.get("/register", response_class=HTMLResponse)
def register_page():
    body = """
    <div class="card">
        <h1>新規登録</h1>
        <form method="post" action="/register">
            <label>username</label>
            <input type="text" name="username" required>

            <label>email</label>
            <input type="email" name="email" required>

            <label>password</label>
            <input type="password" name="password" required>

            <button class="btn" type="submit">登録する</button>
        </form>
        <a class="btn btn-sub" href="/">トップへ戻る</a>
    </div>
    """
    return HTMLResponse(render_page("登録", body))


@app.post("/register")
def register(username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    try:
        create_user(username, email, password)
        user = get_user_by_email(email)
        token = create_session(user["id"])

        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie("session_token", token, httponly=True, samesite="lax")
        return response
    except ValueError as e:
        return HTMLResponse(
            render_page(
                "登録エラー",
                f"<div class='card'><h1>登録エラー</h1><pre>{str(e)}</pre><a class='btn btn-sub' href='/register'>戻る</a></div>",
            ),
            status_code=400,
        )


@app.get("/login", response_class=HTMLResponse)
def login_page():
    body = """
    <div class="card">
        <h1>ログイン</h1>
        <form method="post" action="/login">
            <label>email</label>
            <input type="email" name="email" required>

            <label>password</label>
            <input type="password" name="password" required>

            <button class="btn" type="submit">ログイン</button>
        </form>
        <a class="btn btn-sub" href="/">トップへ戻る</a>
    </div>
    """
    return HTMLResponse(render_page("ログイン", body))


@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    user = get_user_by_email(email)
    if not user or not verify_password(password, user.get("password_hash", "")):
        return HTMLResponse(
            render_page("ログイン失敗", "<div class='card'><h1>メールアドレスかパスワードが違います</h1></div>"),
            status_code=400,
        )

    token = create_session(user["id"])
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("session_token", token, httponly=True, samesite="lax")
    return response


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)

    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("session_token")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    used = get_monthly_used_seconds(user["id"])
    limit_sec = get_user_limit_seconds(user)
    remaining = max(0, limit_sec - used)

    items = get_user_transcriptions(user["id"])
    rows = ""
    for item in items:
        rows += f"""
        <tr>
            <td>{item["id"]}</td>
            <td>{item.get("original_filename") or ""}</td>
            <td>{item.get("duration_seconds") or 0}</td>
            <td>{item.get("created_at") or ""}</td>
            <td>
                <a class="btn btn-sub" href="/history/{item['id']}">見る</a>
                <a class="btn" href="/regenerate/{item['id']}">再整形</a>
            </td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='5'>まだ履歴はありません</td></tr>"

    body = f"""
    <div class="card">
        <h1>マイページ / 履歴</h1>
        <p>ユーザー: <strong>{user.get("username")}</strong> / {user.get("email")}</p>
        <p>プラン: <span class="pill">{user.get("plan") or "free"}</span></p>
        <p>状態: <span class="pill">{user.get("status") or "-"}</span></p>
        <p>今月使用量: <strong>{used} 秒</strong> / {limit_sec} 秒</p>
        <p>残り: <strong>{remaining} 秒</strong></p>

        <a class="btn" href="/buy/standard">standard 購入</a>
        <a class="btn" href="/buy/pro">pro 購入</a>
        <a class="btn" href="/buy/business">business 購入</a>
        <a class="btn btn-green" href="/portal">Customer Portal</a>
        <a class="btn btn-sub" href="/">トップ</a>
        <a class="btn btn-sub" href="/logout">ログアウト</a>
    </div>

    <div class="card">
        <h2>ログイン時の文字起こし</h2>
        <p class="muted">秒数は自動で判定します。自動整形と履歴保存に対応しています。</p>
        <form method="post" action="/transcribe" enctype="multipart/form-data">
            <label>音声ファイル</label>
            <input type="file" name="audio_file" required>
            <button class="btn" type="submit">文字起こしする</button>
        </form>
    </div>

    <div class="card">
        <h2>履歴</h2>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>ファイル名</th>
                    <th>秒数</th>
                    <th>日時</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
    </div>
    """
    return HTMLResponse(render_page("ダッシュボード", body))


@app.get("/history/{transcription_id}", response_class=HTMLResponse)
def history_detail(request: Request, transcription_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    item = get_transcription_by_id(user["id"], transcription_id)
    if not item:
        raise HTTPException(status_code=404, detail="履歴が見つかりません")

    transform_history = get_transform_history(transcription_id, user["id"])
    history_rows = ""
    for h in transform_history:
        history_rows += f"""
        <tr>
            <td>{h['id']}</td>
            <td>{h.get('transform_type') or ''}</td>
            <td>{h.get('created_at') or ''}</td>
            <td><pre>{h.get('transformed_text') or ''}</pre></td>
        </tr>
        """
    if not history_rows:
        history_rows = "<tr><td colspan='4'>まだ変換履歴はありません</td></tr>"

    transform_buttons = ""
    if user.get("plan") != "free":
        transform_buttons = f"""
        <a class="btn" href="/transform/{transcription_id}?type=x_posts">X投稿3案</a>
        <a class="btn" href="/transform/{transcription_id}?type=x_thread">Xスレッド</a>
        <a class="btn" href="/transform/{transcription_id}?type=short_30">ショート30秒</a>
        <a class="btn" href="/transform/{transcription_id}?type=short_60">ショート60秒</a>
        <a class="btn" href="/transform/{transcription_id}?type=blog">ブログ記事</a>
        <a class="btn" href="/transform/{transcription_id}?type=summary_3">3行要約</a>
        <a class="btn" href="/transform/{transcription_id}?type=summary_1min">1分要約</a>
        """
    else:
        transform_buttons = "<p class='muted'>変換機能は有料プランで利用できます。</p>"

    body = f"""
    <div class="card">
        <h1>履歴詳細 #{item['id']}</h1>
        <p>ファイル名: {item.get("original_filename") or ""}</p>
        <p>秒数: {item.get("duration_seconds") or 0}</p>
        <p>日時: {item.get("created_at") or ""}</p>
        <a class="btn btn-sub" href="/dashboard">戻る</a>
        <a class="btn" href="/regenerate/{item['id']}">再整形</a>
    </div>

    <div class="card">
        <h2>生文字起こし</h2>
        <pre>{item.get("raw_text") or ""}</pre>
    </div>

    <div class="card">
        <h2>自動整形後</h2>
        <pre>{item.get("cleaned_text") or ""}</pre>
    </div>

    <div class="card">
        <h2>変換</h2>
        {transform_buttons}
    </div>

    <div class="card">
        <h2>最新の変換結果</h2>
        <p>変換種別: <span class="pill">{item.get("transform_type") or "-"}</span></p>
        <pre>{item.get("transformed_text") or ""}</pre>
    </div>

    <div class="card">
        <h2>変換履歴</h2>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>種別</th>
                    <th>日時</th>
                    <th>内容</th>
                </tr>
            </thead>
            <tbody>
                {history_rows}
            </tbody>
        </table>
    </div>
    """
    return HTMLResponse(render_page("履歴詳細", body))


@app.get("/regenerate/{transcription_id}")
def regenerate(request: Request, transcription_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    item = get_transcription_by_id(user["id"], transcription_id)
    if not item:
        raise HTTPException(status_code=404, detail="履歴が見つかりません")

    try:
        cleaned = clean_transcript_text(item.get("raw_text") or "")

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE transcriptions SET cleaned_text = ? WHERE id = ?", (cleaned, transcription_id))
            conn.commit()

        return RedirectResponse(f"/history/{transcription_id}", status_code=303)

    except Exception as e:
        return HTMLResponse(
            render_page(
                "再生成エラー",
                f"<div class='card'><h1>再生成失敗</h1><pre>{str(e)}</pre><a class='btn btn-sub' href='/history/{transcription_id}'>戻る</a></div>",
            ),
            status_code=500,
        )


@app.get("/transform/{transcription_id}")
def transform_route(request: Request, transcription_id: int, type: str = Query(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    item = get_transcription_by_id(user["id"], transcription_id)
    if not item:
        raise HTTPException(status_code=404, detail="履歴が見つかりません")

    try:
        result = transform_text(
            cleaned_text=item.get("cleaned_text") or "",
            transform_type=type,
            plan=user.get("plan") or "free",
        )

        update_transcription_transform(transcription_id, type, result)
        save_transform_history(transcription_id, user["id"], type, result)

        return RedirectResponse(f"/history/{transcription_id}", status_code=303)

    except Exception as e:
        return HTMLResponse(
            render_page(
                "変換エラー",
                f"<div class='card'><h1>変換失敗</h1><pre>{str(e)}</pre><a class='btn btn-sub' href='/history/{transcription_id}'>戻る</a></div>",
            ),
            status_code=500,
        )


@app.post("/transcribe-public")
async def transcribe_public(audio_file: UploadFile = File(...)):
    if not openai_client:
        return HTMLResponse(
            render_page(
                "APIキー未設定",
                "<div class='card'><h1>OPENAI_API_KEY が未設定です</h1><a class='btn btn-sub' href='/'>戻る</a></div>",
            ),
            status_code=500,
        )

    safe_name, save_path = save_upload(audio_file)

    try:
        content = await audio_file.read()
        with open(save_path, "wb") as f:
            f.write(content)

        duration_seconds = detect_audio_duration_seconds(save_path)
        if duration_seconds > PUBLIC_MAX_SECONDS:
            return HTMLResponse(
                render_page(
                    "上限超過",
                    f"<div class='card'><h1>未ログイン利用は1回 {PUBLIC_MAX_SECONDS} 秒までです</h1><p>今回の音声: {duration_seconds} 秒</p><a class='btn btn-sub' href='/'>戻る</a></div>",
                ),
                status_code=400,
            )

        raw_text = transcribe_file_with_openai(save_path)
        cleaned_text = clean_transcript_text(raw_text)
        cleaned_text = add_free_watermark(cleaned_text)

        return HTMLResponse(
            render_page(
                "文字起こし完了",
                f"""
                <div class="card">
                    <h1>文字起こし完了</h1>
                    <p>ファイル: {audio_file.filename}</p>
                    <p>使用秒数: {duration_seconds}</p>
                    <p class="muted">未ログイン利用のため履歴には保存されていません。変換機能も利用できません。</p>
                    <pre>{cleaned_text}</pre>
                    <a class="btn" href="/">トップへ戻る</a>
                    <a class="btn btn-sub" href="/register">登録して履歴を使う</a>
                    <a class="btn btn-sub" href="/login">ログイン</a>
                </div>
                """
            )
        )

    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(
            render_page(
                "文字起こしエラー",
                f"<div class='card'><h1>文字起こし失敗</h1><pre>{str(e)}</pre><a class='btn btn-sub' href='/'>戻る</a></div>",
            ),
            status_code=500,
        )


@app.post("/transcribe")
async def transcribe_logged_in(request: Request, audio_file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not openai_client:
        return HTMLResponse(
            render_page(
                "APIキー未設定",
                "<div class='card'><h1>OPENAI_API_KEY が未設定です</h1><a class='btn btn-sub' href='/dashboard'>戻る</a></div>",
            ),
            status_code=500,
        )

    safe_name, save_path = save_upload(audio_file)

    try:
        content = await audio_file.read()
        with open(save_path, "wb") as f:
            f.write(content)

        duration_seconds = detect_audio_duration_seconds(save_path)

        used = get_monthly_used_seconds(user["id"])
        limit_sec = get_user_limit_seconds(user)

        if used + duration_seconds > limit_sec:
            return HTMLResponse(
                render_page(
                    "上限超過",
                    f"<div class='card'><h1>今月の上限を超えます</h1><p>使用済み: {used} 秒 / 上限: {limit_sec} 秒 / 今回: {duration_seconds} 秒</p><a class='btn btn-sub' href='/dashboard'>戻る</a></div>",
                ),
                status_code=400,
            )

        raw_text = transcribe_file_with_openai(save_path)
        cleaned_text = clean_transcript_text(raw_text)

        record_id = save_transcription(
            user_id=user["id"],
            filename=safe_name,
            original_filename=audio_file.filename,
            duration_seconds=duration_seconds,
            raw_text=raw_text,
            cleaned_text=cleaned_text,
            transform_type=None,
            transformed_text=None,
        )

        return RedirectResponse(f"/history/{record_id}", status_code=303)

    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(
            render_page(
                "文字起こしエラー",
                f"<div class='card'><h1>文字起こし失敗</h1><pre>{str(e)}</pre><a class='btn btn-sub' href='/dashboard'>戻る</a></div>",
            ),
            status_code=500,
        )


@app.get("/health", response_class=HTMLResponse)
def health():
    data = {
        "status": "ok",
        "stripe_api_key_loaded": bool(STRIPE_SECRET_KEY),
        "webhook_secret_loaded": bool(STRIPE_WEBHOOK_SECRET),
        "openai_api_key_loaded": bool(OPENAI_API_KEY),
        "base_url": BASE_URL,
        "admin_email_loaded": bool(ADMIN_EMAIL),
    }
    body = f"""
    <div class="card">
        <h1>health</h1>
        <pre>{json.dumps(data, ensure_ascii=False, indent=2)}</pre>
        <a class="btn btn-sub" href="/">トップへ戻る</a>
    </div>
    """
    return HTMLResponse(render_page("health", body))


@app.get("/users", response_class=HTMLResponse)
def users_view(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not is_admin(user):
        return HTMLResponse(
            render_page(
                "権限エラー",
                "<div class='card'><h1>このページは管理者のみ閲覧できます</h1><a class='btn btn-sub' href='/dashboard'>戻る</a></div>",
            ),
            status_code=403,
        )

    users = get_all_users()
    pretty = json.dumps(users, ensure_ascii=False, indent=2)

    rows = ""
    for u in users:
        rows += f"""
        <tr>
            <td>{u.get("id", "")}</td>
            <td>{u.get("username", "") or ""}</td>
            <td>{u.get("email", "") or ""}</td>
            <td><span class="pill">{u.get("plan", "") or "-"}</span></td>
            <td><span class="pill">{u.get("status", "") or "-"}</span></td>
            <td>{u.get("stripe_customer_id", "") or ""}</td>
            <td>{u.get("stripe_subscription_id", "") or ""}</td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='7'>まだ保存されたユーザーはありません</td></tr>"

    body = f"""
    <div class="card">
        <h1>保存ユーザー一覧</h1>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>username</th>
                    <th>email</th>
                    <th>plan</th>
                    <th>status</th>
                    <th>customer_id</th>
                    <th>subscription_id</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>

    <div class="card">
        <h2>JSON表示</h2>
        <pre>{pretty}</pre>
        <a class="btn btn-sub" href="/">トップへ戻る</a>
    </div>
    """
    return HTMLResponse(render_page("users", body))


@app.get("/buy/{plan}")
def buy(request: Request, plan: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if plan not in PRICE_MAP:
        return HTMLResponse(
            render_page("エラー", "<div class='card'><h1>プランが不正です</h1></div>"),
            status_code=400,
        )

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": PRICE_MAP[plan], "quantity": 1}],
            customer_email=user["email"],
            metadata={
                "username": user["username"] or "",
                "email": user["email"],
                "plan": plan,
            },
            subscription_data={
                "metadata": {
                    "username": user["username"] or "",
                    "email": user["email"],
                    "plan": plan,
                }
            },
            success_url=f"{BASE_URL}/success?plan={plan}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/cancel",
        )
        return RedirectResponse(url=session.url, status_code=303)

    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(
            render_page(
                "Stripeエラー",
                f"<div class='card'><h1>Checkout作成失敗</h1><pre>{str(e)}</pre><a class='btn btn-sub' href='/dashboard'>戻る</a></div>",
            ),
            status_code=500,
        )


@app.get("/success", response_class=HTMLResponse)
def success(plan: str = "", session_id: str = ""):
    body = f"""
    <div class="card">
        <h1>決済成功</h1>
        <p>プラン: <strong>{plan}</strong></p>
        <p>session_id: <strong>{session_id}</strong></p>
        <a class="btn" href="/dashboard">ダッシュボード</a>
        <a class="btn btn-green" href="/portal">Customer Portal</a>
        <a class="btn btn-sub" href="/">トップへ戻る</a>
    </div>
    """
    return HTMLResponse(render_page("成功", body))


@app.get("/cancel", response_class=HTMLResponse)
def cancel():
    body = """
    <div class="card">
        <h1>決済キャンセル</h1>
        <a class="btn btn-sub" href="/">トップへ戻る</a>
    </div>
    """
    return HTMLResponse(render_page("キャンセル", body))


@app.get("/portal")
def portal(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    try:
        customer_id = user.get("stripe_customer_id")
        if not customer_id:
            raise HTTPException(status_code=400, detail="stripe customer not found")

        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=BASE_URL,
        )
        return RedirectResponse(session.url, status_code=303)

    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(
            render_page(
                "Portalエラー",
                f"<div class='card'><h1>Customer Portal エラー</h1><pre>{str(e)}</pre><a class='btn btn-sub' href='/dashboard'>戻る</a></div>",
            ),
            status_code=500,
        )


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        print("=== WEBHOOK RECEIVED ===")
        print("event id =", event["id"])
        print("event type =", event["type"])
    except Exception as e:
        print("signature error =", repr(e))
        traceback.print_exc()
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=400)

    try:
        event_id = event["id"]
        event_type = event["type"]
        obj = event["data"]["object"]

        if event_already_processed(event_id):
            print("duplicate event skipped =", event_id)
            return JSONResponse({"status": "ok", "duplicate": True})

        if event_type == "checkout.session.completed":
            session = obj

            metadata = getattr(session, "metadata", None)
            username = meta_value(metadata, "username")
            email = meta_value(metadata, "email")
            plan = meta_value(metadata, "plan")

            customer_id = getattr(session, "customer", None)
            subscription_id = getattr(session, "subscription", None)

            customer_details = getattr(session, "customer_details", None)
            email_from_details = getattr(customer_details, "email", None) if customer_details else None
            final_email = email_from_details or email

            upsert_user_subscription(
                username=username,
                email=final_email,
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                plan=plan,
                status="active",
            )
            print("checkout.session.completed saved ok")

        elif event_type == "invoice.paid":
            customer_id = getattr(obj, "customer", None)
            subscription_id = getattr(obj, "subscription", None)
            update_user_by_customer(
                customer_id,
                status="active",
                stripe_subscription_id=subscription_id,
            )
            print("invoice.paid updated user")

        elif event_type == "invoice.payment_failed":
            customer_id = getattr(obj, "customer", None)
            update_user_by_customer(customer_id, status="past_due")
            print("invoice.payment_failed updated user")

        elif event_type == "customer.subscription.deleted":
            customer_id = getattr(obj, "customer", None)
            update_user_by_customer(customer_id, status="canceled")
            print("customer.subscription.deleted updated user")

        elif event_type == "customer.subscription.updated":
            customer_id = getattr(obj, "customer", None)
            subscription_id = getattr(obj, "id", None)
            status = getattr(obj, "status", None)
            current_period_end = str(getattr(obj, "current_period_end", None))
            update_user_by_customer(
                customer_id,
                status=status,
                stripe_subscription_id=subscription_id,
                current_period_end=current_period_end,
            )
            print("customer.subscription.updated updated user")

        else:
            print("ignored event =", event_type)

        mark_event_processed(event_id, event_type)
        return JSONResponse({"status": "ok"})

    except Exception as e:
        print("processing error =", repr(e))
        traceback.print_exc()
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)