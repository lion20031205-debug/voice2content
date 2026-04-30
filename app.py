import os
import sqlite3
from contextlib import closing
from uuid import uuid4
from datetime import datetime, timezone

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from openai import OpenAI
from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer
from mutagen import File as MutagenFile

load_dotenv()

app = FastAPI()

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").lower()
EMAIL_TOKEN_SECRET = os.getenv("EMAIL_TOKEN_SECRET", "dev_email_secret")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

stripe.api_key = STRIPE_SECRET_KEY
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
email_serializer = URLSafeTimedSerializer(EMAIL_TOKEN_SECRET)

DB_PATH = "app.db"

SERVICE_NAME = "Voice2Content"
SERVICE_NAME_JP = "Voice2Content（ボイコン）"
SERVICE_TAGLINE = "喋るだけでコンテンツ完成。"

PRICE_MAP = {
    "standard": "price_1TQEtgRoLzdrUUOZ9FKsuYhW",
    "pro": "price_1TQEteRoLzdrUUOZZ5o9IzG3",
    "business": "price_1TQEteRoLzdrUUOZmHJ2JZPU",
}

PLAN_LIMITS_SECONDS = {
    "free": 10 * 60,
    "standard": 120 * 60,
    "pro": 600 * 60,
    "business": 100000 * 60,
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            email TEXT UNIQUE,
            password_hash TEXT,
            email_verified INTEGER DEFAULT 0,
            role TEXT DEFAULT 'user',
            plan TEXT DEFAULT 'free',
            status TEXT DEFAULT 'inactive',
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT UNIQUE,
            csrf_token TEXT,
            user_id INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS transcriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            duration_seconds INTEGER DEFAULT 0,
            raw_text TEXT,
            cleaned_text TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS transforms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcription_id INTEGER,
            user_id INTEGER,
            transform_type TEXT,
            result_text TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

        conn.commit()


@app.on_event("startup")
def startup():
    init_db()


def page(title, body):
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
                background: linear-gradient(180deg, #fff8f3 0%, #fffaf7 100%);
                color: #3c2a21;
                padding: 0 20px;
            }}
            .card {{
                background: #fffdfb;
                padding: 24px;
                border-radius: 18px;
                box-shadow: 0 8px 24px rgba(120,72,32,0.10);
                border: 1px solid #f3d9c5;
                margin-bottom: 20px;
            }}
            .hero {{
                padding: 48px 32px;
                background: linear-gradient(135deg, #fff1e6 0%, #fffdfb 58%, #ffe7d1 100%);
                border-radius: 26px;
                border: 1px solid #f3d9c5;
                box-shadow: 0 14px 34px rgba(120,72,32,0.12);
                margin-bottom: 24px;
            }}
            .hero h1 {{
                font-size: 46px;
                line-height: 1.18;
                margin: 14px 0;
            }}
            .hero p {{
                font-size: 18px;
                line-height: 1.8;
                color: #6b4a3a;
            }}
            .brand {{
                font-size: 26px;
                font-weight: bold;
                color: #9a3412;
                margin-bottom: 8px;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 16px;
            }}
            .mini {{
                background: #fffaf7;
                border: 1px solid #f3d9c5;
                border-radius: 16px;
                padding: 18px;
            }}
            input, select {{
                display: block;
                width: 100%;
                max-width: 520px;
                padding: 12px;
                margin: 8px 0 16px;
                border-radius: 10px;
                border: 1px solid #e7cbb7;
                background: #fffaf7;
            }}
            button, .btn {{
                display: inline-block;
                background: #d97706;
                color: white;
                padding: 12px 18px;
                border-radius: 10px;
                border: none;
                text-decoration: none;
                font-weight: bold;
                cursor: pointer;
                margin: 6px 6px 6px 0;
            }}
            .sub {{ background: #9a6b4f; }}
            .green {{ background: #b45309; }}
            pre {{
                background: #4a2f27;
                color: #fff7ed;
                padding: 16px;
                border-radius: 12px;
                white-space: pre-wrap;
                word-break: break-word;
            }}
            .pill {{
                background: #fde7d7;
                color: #9a3412;
                padding: 4px 10px;
                border-radius: 999px;
                font-weight: bold;
            }}
            .muted {{
                color: #8b6b5c;
            }}
            .price {{
                font-size: 26px;
                font-weight: bold;
                color: #9a3412;
            }}
            .step {{
                font-size: 28px;
                font-weight: bold;
                color: #d97706;
            }}
        </style>
        <script>
            function copyText(id) {{
                const el = document.getElementById(id);
                navigator.clipboard.writeText(el.innerText);
                alert("コピーしました");
            }}
        </script>
    </head>
    <body>{body}</body>
    </html>
    """


def hash_password(password):
    return pwd_context.hash(password)


def verify_password(password, hashed):
    if not hashed:
        return False
    return pwd_context.verify(password, hashed)


def get_user_by_email(email):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT users.*, sessions.csrf_token
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.session_token = ?
        """, (token,))
        row = cur.fetchone()
        return dict(row) if row else None


def create_session(user_id):
    session_token = str(uuid4())
    csrf_token = str(uuid4())

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sessions (session_token, csrf_token, user_id) VALUES (?, ?, ?)",
            (session_token, csrf_token, user_id)
        )
        conn.commit()

    return session_token


def check_csrf(user, csrf_token):
    return csrf_token and user and csrf_token == user.get("csrf_token")


def send_verification_email(email):
    token = email_serializer.dumps({"email": email})
    url = f"{BASE_URL}/verify?token={token}"
    print("認証URL（開発用）:", url)
    return url


def detect_duration(path):
    audio = MutagenFile(path)
    if audio and hasattr(audio, "info") and hasattr(audio.info, "length"):
        return max(1, int(audio.info.length))
    return 60


def get_monthly_used_seconds(user_id):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(duration_seconds), 0) AS total
            FROM transcriptions
            WHERE user_id = ?
              AND strftime('%Y-%m', created_at) = ?
        """, (user_id, month))
        row = cur.fetchone()
        return int(row["total"] or 0)


def user_limit(user):
    return PLAN_LIMITS_SECONDS.get(user.get("plan") or "free", 600)


def transcribe_file(path):
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY が未設定です")

    with open(path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f
        )

    return result.text


def clean_text(raw_text):
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY が未設定です")

    prompt = f"""
以下の文字起こしを読みやすく整形してください。

条件:
- 「えー」「あのー」「そのー」などを削除
- 句読点を付ける
- 重複や言い直しを自然に整理
- 内容は勝手に追加しない
- 読みやすい日本語にする

文字起こし:
{raw_text}
"""
    res = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )
    return res.output_text


def transform_content(cleaned_text, transform_type, plan):
    if plan == "free":
        raise ValueError("変換機能は有料プラン限定です")

    prompts = {
        "x_posts": f"以下をX投稿140文字以内で3案作ってください。\n\n{cleaned_text}",
        "x_thread": f"以下をXスレッド5投稿にしてください。最初は強いフック。\n\n{cleaned_text}",
        "short_30": f"以下をYouTubeショート30秒台本にしてください。冒頭フック付き。\n\n{cleaned_text}",
        "short_60": f"以下をYouTubeショート60秒台本にしてください。冒頭フック付き。\n\n{cleaned_text}",
        "blog": f"以下をブログ記事にしてください。見出しあり、1000〜3000文字。\n\n{cleaned_text}",
        "summary_3": f"以下を3行で要約してください。\n\n{cleaned_text}",
        "summary_1min": f"以下を1分で読める要約にしてください。\n\n{cleaned_text}",
    }

    if transform_type not in prompts:
        raise ValueError("不正な変換タイプです")

    res = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=prompts[transform_type]
    )
    return res.output_text


def safe_meta(obj, key):
    metadata = getattr(obj, "metadata", None)
    if not metadata:
        return None
    try:
        return metadata[key]
    except Exception:
        return None


def update_user_subscription(email=None, customer=None, subscription=None, plan=None, status=None):
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        if email:
            cur.execute("""
                UPDATE users
                SET plan = COALESCE(?, plan),
                    status = COALESCE(?, status),
                    stripe_customer_id = COALESCE(?, stripe_customer_id),
                    stripe_subscription_id = COALESCE(?, stripe_subscription_id)
                WHERE email = ?
            """, (plan, status, customer, subscription, email))

        elif customer:
            cur.execute("""
                UPDATE users
                SET status = COALESCE(?, status),
                    stripe_subscription_id = COALESCE(?, stripe_subscription_id)
                WHERE stripe_customer_id = ?
            """, (status, subscription, customer))

        conn.commit()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)

    if user:
        cta = f"""
        <a class="btn" href="/dashboard">ダッシュボードへ</a>
        <a class="btn green" href="/portal">プラン管理</a>
        <a class="btn sub" href="/logout">ログアウト</a>
        """
    else:
        cta = """
        <a class="btn" href="/register">無料で始める</a>
        <a class="btn sub" href="/login">ログイン</a>
        """

    body = f"""
    <section class="hero">
        <div class="brand">{SERVICE_NAME_JP}</div>
        <span class="pill">音声からコンテンツ生成</span>
        <h1>喋るだけで<br>コンテンツ完成。</h1>
        <p>
            音声をアップロードするだけで、
            文字起こし → 自動整形 → X投稿・YouTubeショート台本・ブログに変換。
            面倒な編集作業を、数分で終わらせます。
        </p>
        {cta}
    </section>

    <div class="card">
        <h2>こんな感じに変わる</h2>
        <div class="grid">
            <div class="mini">
                <h3>Before</h3>
                <pre>えー今日はですね副業について話していこうかなと思うんですけどあのー時間がなくて...</pre>
            </div>
            <div class="mini">
                <h3>自動整形</h3>
                <pre>今日は副業について話します。時間がない中でも効率よく進める方法について解説します。</pre>
            </div>
            <div class="mini">
                <h3>投稿化</h3>
                <pre>副業で結果が出ない人の共通点は「時間がない」と思い込んでること。

でも実は違う。
必要なのは“やり方”です。</pre>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>無料では文字起こしまで</h2>
        <p>
            無料プランでは、音声の文字起こしと自動整形まで使えます。
            <br><b>X投稿・YouTubeショート台本・ブログ記事・要約への変換は、有料プランで解放されます。</b>
        </p>
        <a class="btn" href="/register">まず無料で試す</a>
        <a class="btn green" href="/buy/standard">変換機能を使う（¥980/月）</a>
    </div>

    <div class="card">
        <h2>{SERVICE_NAME_JP} でできること</h2>
        <div class="grid">
            <div class="mini"><h3>文字起こし</h3><p>音声をテキスト化</p></div>
            <div class="mini"><h3>自動整形</h3><p>不要語削除＋読みやすく</p></div>
            <div class="mini"><h3>X投稿生成</h3><p>140文字×3案</p></div>
            <div class="mini"><h3>ショート台本</h3><p>30秒 / 60秒</p></div>
            <div class="mini"><h3>ブログ生成</h3><p>1000〜3000文字</p></div>
            <div class="mini"><h3>要約</h3><p>3行 / 1分版</p></div>
        </div>
    </div>

    <div class="card">
        <h2>使い方は3ステップ</h2>
        <div class="grid">
            <div class="mini">
                <div class="step">01</div>
                <h3>音声をアップロード</h3>
                <p>話した音声ファイルを選ぶだけ。</p>
            </div>
            <div class="mini">
                <div class="step">02</div>
                <h3>自動で整形</h3>
                <p>不要な言葉を消して読みやすく。</p>
            </div>
            <div class="mini">
                <div class="step">03</div>
                <h3>投稿・台本・記事に変換</h3>
                <p>ボタン1つでコンテンツ化。</p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>料金プラン</h2>
        <div class="grid">
            <div class="mini">
                <h3>フリー</h3>
                <div class="price">¥0</div>
                <p>月10分まで</p>
                <p>文字起こし・自動整形</p>
                <p>透かしあり</p>
            </div>
            <div class="mini">
                <h3>スタンダード</h3>
                <div class="price">¥980/月</div>
                <p>月120分</p>
                <p>全変換機能</p>
                <p>X投稿・要約・台本</p>
            </div>
            <div class="mini">
                <h3>プロ</h3>
                <div class="price">¥2,980/月</div>
                <p>月600分</p>
                <p>長時間対応</p>
                <p>ブログ記事生成</p>
            </div>
            <div class="mini">
                <h3>ビジネス</h3>
                <div class="price">¥9,800/月</div>
                <p>大容量</p>
                <p>チーム共有予定</p>
                <p>法人向け</p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>無料で試す</h2>
        <p class="muted">ログインなしでも60秒まで文字起こしできます。</p>
        <form action="/transcribe-public" method="post" enctype="multipart/form-data">
            <p>音声ファイル</p>
            <input type="file" name="audio_file" required>
            <button>無料で文字起こしする</button>
        </form>
    </div>
    """
    return HTMLResponse(page(f"{SERVICE_NAME_JP} - {SERVICE_TAGLINE}", body))


@app.get("/register", response_class=HTMLResponse)
def register_page():
    return HTMLResponse(page("登録", """
    <div class="card">
        <h1>新規登録</h1>
        <form method="post" action="/register">
            <p>ユーザー名</p>
            <input name="username" required>
            <p>メールアドレス</p>
            <input name="email" required>
            <p>パスワード</p>
            <input name="password" type="password" required>
            <button>登録</button>
        </form>
        <a class="btn sub" href="/">戻る</a>
    </div>
    """))


@app.post("/register", response_class=HTMLResponse)
def register(username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    if len(password) < 8:
        return HTMLResponse(page("登録エラー", "<h1>パスワードは8文字以上</h1>"), status_code=400)

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        if cur.fetchone():
            return HTMLResponse(page("登録エラー", """
            <h1>このメールは既に登録されています</h1>
            <a class="btn" href="/login">ログインへ</a>
            """), status_code=400)

        role = "admin" if email.lower() == ADMIN_EMAIL else "user"

        cur.execute("""
            INSERT INTO users (username, email, password_hash, role)
            VALUES (?, ?, ?, ?)
        """, (username, email, hash_password(password), role))
        conn.commit()

    url = send_verification_email(email)

    return HTMLResponse(page("登録OK", f"""
    <h1>登録OK</h1>
    <p>下の認証リンクをクリックしてください。</p>
    <a class="btn" href="{url}">メール認証する</a>
    <a class="btn sub" href="/login">ログインへ</a>
    """))


@app.get("/verify", response_class=HTMLResponse)
def verify(token: str):
    try:
        data = email_serializer.loads(token, max_age=86400)
        email = data["email"]

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET email_verified = 1 WHERE email = ?", (email,))
            conn.commit()

        return HTMLResponse(page("認証OK", """
        <h1>メール認証OK</h1>
        <a class="btn" href="/login">ログインへ</a>
        """))
    except Exception as e:
        return HTMLResponse(page("認証失敗", f"<pre>{e}</pre>"), status_code=400)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(page("ログイン", """
    <div class="card">
        <h1>ログイン</h1>
        <form method="post" action="/login">
            <p>メールアドレス</p>
            <input name="email" required>
            <p>パスワード</p>
            <input name="password" type="password" required>
            <button>ログイン</button>
        </form>
        <a class="btn sub" href="/">戻る</a>
    </div>
    """))


@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    user = get_user_by_email(email)

    if not user:
        return HTMLResponse(page("ログイン失敗", "<h1>ユーザーが見つかりません</h1>"), status_code=400)

    if not verify_password(password, user["password_hash"]):
        return HTMLResponse(page("ログイン失敗", "<h1>パスワードが違います</h1>"), status_code=400)

    if not user["email_verified"]:
        return HTMLResponse(page("未認証", "<h1>メール認証してください</h1>"), status_code=403)

    token = create_session(user["id"])
    res = RedirectResponse("/dashboard", status_code=303)
    res.set_cookie("session_token", token, httponly=True, samesite="lax")
    return res


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session_token")

    if token:
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE session_token = ?", (token,))
            conn.commit()

    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("session_token")
    return res


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    used = get_monthly_used_seconds(user["id"])
    limit = user_limit(user)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM transcriptions
            WHERE user_id = ?
            ORDER BY id DESC
        """, (user["id"],))
        rows = cur.fetchall()

    history = ""
    for r in rows:
        history += f"""
        <div class="card">
            <p>#{r["id"]} / {r["filename"]} / {r["duration_seconds"]}秒</p>
            <a class="btn" href="/history/{r["id"]}">詳細を見る</a>
        </div>
        """

    if not history:
        history = "<p>まだ履歴はありません</p>"

    return HTMLResponse(page("ダッシュボード", f"""
    <div class="card">
        <h1>{SERVICE_NAME_JP} ダッシュボード</h1>
        <p>ログイン中: {user["email"]}</p>
        <p>プラン: <span class="pill">{user["plan"]}</span></p>
        <p>今月使用量: {used}秒 / {limit}秒</p>

        <a class="btn" href="/buy/standard">Standard購入</a>
        <a class="btn" href="/buy/pro">Pro購入</a>
        <a class="btn" href="/buy/business">Business購入</a>
        <a class="btn green" href="/portal">プラン管理・解約</a>
    </div>

    <div class="card">
        <h2>文字起こし</h2>
        <form action="/transcribe" method="post" enctype="multipart/form-data">
            <input type="hidden" name="csrf_token" value="{user["csrf_token"]}">
            <input type="file" name="audio_file" required>
            <button>実行</button>
        </form>
    </div>

    <div class="card">
        <h2>履歴</h2>
        {history}
    </div>
    """))


@app.post("/transcribe-public")
async def transcribe_public(audio_file: UploadFile = File(...)):
    if not openai_client:
        return HTMLResponse(page("エラー", "<h1>OPENAI_API_KEY が未設定です</h1>"), status_code=500)

    filename = f"temp_{uuid4()}_{audio_file.filename}"

    with open(filename, "wb") as f:
        f.write(await audio_file.read())

    duration = detect_duration(filename)

    if duration > 60:
        return HTMLResponse(page("制限", "<h1>未ログインは60秒までです</h1>"), status_code=400)

    raw = transcribe_file(filename)
    cleaned = clean_text(raw) + f"\n\n---\n無料版で生成されました / {SERVICE_NAME_JP}"

    return HTMLResponse(page("文字起こし結果", f"""
    <h1>{SERVICE_NAME_JP} 文字起こし結果</h1>
    <button onclick="copyText('r')">コピー</button>
    <pre id="r">{cleaned}</pre>
    <a class="btn sub" href="/">戻る</a>
    """))


@app.post("/transcribe")
async def transcribe_logged_in(
    request: Request,
    audio_file: UploadFile = File(...),
    csrf_token: str = Form(...)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not check_csrf(user, csrf_token):
        return HTMLResponse(page("エラー", "<h1>CSRFエラー</h1>"), status_code=403)

    filename = f"temp_{uuid4()}_{audio_file.filename}"

    with open(filename, "wb") as f:
        f.write(await audio_file.read())

    duration = detect_duration(filename)
    used = get_monthly_used_seconds(user["id"])
    limit = user_limit(user)

    if used + duration > limit:
        return HTMLResponse(page("上限超過", f"<h1>今月の上限を超えます</h1><p>{used}+{duration}>{limit}</p>"), status_code=400)

    raw = transcribe_file(filename)
    cleaned = clean_text(raw)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO transcriptions (user_id, filename, duration_seconds, raw_text, cleaned_text)
            VALUES (?, ?, ?, ?, ?)
        """, (user["id"], audio_file.filename, duration, raw, cleaned))
        conn.commit()
        tid = cur.lastrowid

    return RedirectResponse(f"/history/{tid}", status_code=303)


@app.get("/history/{tid}", response_class=HTMLResponse)
def history_detail(request: Request, tid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM transcriptions WHERE id = ? AND user_id = ?", (tid, user["id"]))
        item = cur.fetchone()

        cur.execute("SELECT * FROM transforms WHERE transcription_id = ? ORDER BY id DESC", (tid,))
        transforms = cur.fetchall()

    if not item:
        raise HTTPException(status_code=404)

    buttons = ""
    if user["plan"] != "free":
        for label, t in [
            ("X投稿3案", "x_posts"),
            ("Xスレッド", "x_thread"),
            ("ショート30秒", "short_30"),
            ("ショート60秒", "short_60"),
            ("ブログ", "blog"),
            ("3行要約", "summary_3"),
            ("1分要約", "summary_1min"),
        ]:
            buttons += f"""
            <form method="post" action="/transform/{tid}" style="display:inline;">
                <input type="hidden" name="csrf_token" value="{user["csrf_token"]}">
                <input type="hidden" name="transform_type" value="{t}">
                <button>{label}</button>
            </form>
            """
    else:
        buttons = "<p>変換機能は有料プラン限定です。</p>"

    trans_html = ""
    for tr in transforms:
        trans_html += f"""
        <div class="card">
            <p>{tr["transform_type"]}</p>
            <button onclick="copyText('tr{tr["id"]}')">コピー</button>
            <pre id="tr{tr["id"]}">{tr["result_text"]}</pre>
        </div>
        """

    return HTMLResponse(page("履歴詳細", f"""
    <div class="card">
        <h1>{SERVICE_NAME_JP} 履歴詳細 #{tid}</h1>
        <a class="btn sub" href="/dashboard">戻る</a>
    </div>

    <div class="card">
        <h2>生文字起こし</h2>
        <button onclick="copyText('raw')">コピー</button>
        <pre id="raw">{item["raw_text"]}</pre>
    </div>

    <div class="card">
        <h2>自動整形後</h2>
        <button onclick="copyText('clean')">コピー</button>
        <pre id="clean">{item["cleaned_text"]}</pre>
    </div>

    <div class="card">
        <h2>変換</h2>
        {buttons}
    </div>

    <div class="card">
        <h2>変換履歴</h2>
        {trans_html if trans_html else "<p>まだ変換履歴はありません</p>"}
    </div>
    """))


@app.post("/transform/{tid}")
def transform_route(
    request: Request,
    tid: int,
    transform_type: str = Form(...),
    csrf_token: str = Form(...)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not check_csrf(user, csrf_token):
        return HTMLResponse(page("エラー", "<h1>CSRFエラー</h1>"), status_code=403)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM transcriptions WHERE id = ? AND user_id = ?", (tid, user["id"]))
        item = cur.fetchone()

    if not item:
        raise HTTPException(status_code=404)

    try:
        result = transform_content(item["cleaned_text"], transform_type, user["plan"])
    except Exception as e:
        return HTMLResponse(page("変換エラー", f"<pre>{e}</pre>"), status_code=400)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO transforms (transcription_id, user_id, transform_type, result_text)
            VALUES (?, ?, ?, ?)
        """, (tid, user["id"], transform_type, result))
        conn.commit()

    return RedirectResponse(f"/history/{tid}", status_code=303)


@app.get("/users")
def users(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    if user["role"] != "admin":
        raise HTTPException(status_code=403)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, email, email_verified, role, plan, status FROM users")
        rows = cur.fetchall()

    return JSONResponse([dict(r) for r in rows])


@app.get("/buy/{plan}")
def buy(request: Request, plan: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not user["email_verified"]:
        return HTMLResponse(page("未認証", "<h1>メール認証してください</h1>"), status_code=403)

    if plan not in PRICE_MAP:
        return HTMLResponse(page("エラー", "<h1>不正なプランです</h1>"), status_code=400)

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": PRICE_MAP[plan], "quantity": 1}],
            customer_email=user["email"],
            metadata={
                "email": user["email"],
                "username": user["username"],
                "plan": plan,
            },
            subscription_data={
                "metadata": {
                    "email": user["email"],
                    "username": user["username"],
                    "plan": plan,
                }
            },
            success_url=f"{BASE_URL}/success",
            cancel_url=f"{BASE_URL}/cancel",
        )

        return RedirectResponse(session.url, status_code=303)

    except Exception as e:
        return HTMLResponse(page("Stripeエラー", f"<pre>{e}</pre>"), status_code=500)


@app.get("/portal")
def portal(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    customer_id = user.get("stripe_customer_id")

    if not customer_id:
        return HTMLResponse(page(
            "Portalエラー",
            """
            <h1>まだ有料プランがありません</h1>
            <p>先にStandard / Pro / Business のどれかを購入してください。</p>
            <a class="btn" href="/dashboard">戻る</a>
            """
        ), status_code=400)

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{BASE_URL}/dashboard",
        )
        return RedirectResponse(session.url, status_code=303)

    except Exception as e:
        return HTMLResponse(page("Portalエラー", f"<pre>{e}</pre>"), status_code=500)


@app.get("/success")
def success():
    return HTMLResponse(page("決済成功", """
    <h1>決済成功</h1>
    <p>Webhookが成功するとプランが反映されます。</p>
    <a class="btn" href="/dashboard">ダッシュボードへ</a>
    """))


@app.get("/cancel")
def cancel():
    return HTMLResponse(page("キャンセル", """
    <h1>決済キャンセル</h1>
    <a class="btn" href="/dashboard">戻る</a>
    """))

@app.get("/legal", response_class=HTMLResponse)
def legal_page():
    return HTMLResponse(page("特定商取引法に基づく表記", """
    <div class="card">
        <h1>特定商取引法に基づく表記</h1>

        <table style="width:100%; border-collapse:collapse;">
            <tr><th style="border:1px solid #f3d9c5; padding:12px; width:30%;">販売事業者</th><td style="border:1px solid #f3d9c5; padding:12px;">Voice2Content</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">運営責任者</th><td style="border:1px solid #f3d9c5; padding:12px;">泉</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">所在地</th><td style="border:1px solid #f3d9c5; padding:12px;">大阪府大阪市西区九条2-13-20</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">電話番号</th><td style="border:1px solid #f3d9c5; padding:12px;">070-4222-7450（受付時間：平日10:00〜18:00）</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">メールアドレス</th><td style="border:1px solid #f3d9c5; padding:12px;">support@voice2contentai.com</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">販売URL</th><td style="border:1px solid #f3d9c5; padding:12px;">https://app.voice2contentai.com</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">販売価格</th><td style="border:1px solid #f3d9c5; padding:12px;">各プランページに表示された金額</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">商品・サービス内容</th><td style="border:1px solid #f3d9c5; padding:12px;">音声ファイルをアップロードすることで、自動文字起こし、SNS投稿文生成、YouTubeショート台本生成、ブログ記事生成、要約生成などを行うWebサービスです。</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">追加料金</th><td style="border:1px solid #f3d9c5; padding:12px;">インターネット通信料はお客様のご負担となります。</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">支払方法</th><td style="border:1px solid #f3d9c5; padding:12px;">クレジットカード決済（Stripe）</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">決済期間</th><td style="border:1px solid #f3d9c5; padding:12px;">クレジットカード決済は即時処理されます。</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">サービス提供時期</th><td style="border:1px solid #f3d9c5; padding:12px;">決済完了後、直ちにご利用いただけます。</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">返品・キャンセル（通常）</th><td style="border:1px solid #f3d9c5; padding:12px;">デジタルサービスの性質上、決済後の返金には原則対応しておりません。サブスクリプションは次回更新日前までに解約可能です。</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">返品・キャンセル（不良時）</th><td style="border:1px solid #f3d9c5; padding:12px;">サービスに重大な不具合がある場合は、サポートへご連絡ください。状況に応じて返金または補償対応を行います。</td></tr>
            <tr><th style="border:1px solid #f3d9c5; padding:12px;">動作環境</th><td style="border:1px solid #f3d9c5; padding:12px;">インターネット接続環境およびWebブラウザが必要です。</td></tr>
        </table>

        <br>
        <a class="btn sub" href="/">トップへ戻る</a>
    </div>
    """))
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig,
            STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("WEBHOOK SIGNATURE ERROR =", repr(e))
        return JSONResponse({"error": str(e)}, status_code=400)

    event_type = event["type"]
    obj = event["data"]["object"]

    print("=== WEBHOOK ===")
    print("type =", event_type)

    try:
        if event_type == "checkout.session.completed":
            email = safe_meta(obj, "email")
            plan = safe_meta(obj, "plan")
            customer = getattr(obj, "customer", None)
            subscription = getattr(obj, "subscription", None)

            update_user_subscription(
                email=email,
                customer=customer,
                subscription=subscription,
                plan=plan,
                status="active",
            )

            print("checkout completed user updated")

        elif event_type == "invoice.paid":
            customer = getattr(obj, "customer", None)
            subscription = getattr(obj, "subscription", None)

            update_user_subscription(
                customer=customer,
                subscription=subscription,
                status="active",
            )

            print("invoice paid updated")

        elif event_type == "invoice.payment_failed":
            customer = getattr(obj, "customer", None)

            update_user_subscription(
                customer=customer,
                status="past_due",
            )

            print("payment failed updated")

        elif event_type == "customer.subscription.updated":
            customer = getattr(obj, "customer", None)
            subscription = getattr(obj, "id", None)
            status = getattr(obj, "status", None)

            update_user_subscription(
                customer=customer,
                subscription=subscription,
                status=status,
            )

            print("subscription updated =", status)

        elif event_type == "customer.subscription.deleted":
            customer = getattr(obj, "customer", None)

            update_user_subscription(
                customer=customer,
                status="canceled",
            )

            print("subscription canceled")

        else:
            print("ignored =", event_type)

        return JSONResponse({"ok": True})

    except Exception as e:
        print("WEBHOOK HANDLER ERROR =", repr(e))
        return JSONResponse({"error": str(e)}, status_code=500)