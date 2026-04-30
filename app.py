from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI()

def page(title, body):
    return f"""
    <html>
    <head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
    body {{ font-family: Arial; max-width: 800px; margin: 40px auto; }}
    .card {{ padding:20px; border:1px solid #ddd; border-radius:10px; }}
    .btn {{ padding:10px 20px; background:#333; color:white; text-decoration:none; }}
    </style>
    </head>
    <body>
    {body}
    </body>
    </html>
    """

@app.get("/")
def home():
    return HTMLResponse(page("Home", "<h1>Voice2Content</h1>"))

@app.get("/legal", response_class=HTMLResponse)
def legal_page():
    return HTMLResponse(page("特定商取引法に基づく表記", """
    <div class="card">
        <h1>特定商取引法に基づく表記</h1>

        <table style="width:100%; border-collapse:collapse;">
            <tr><th>販売事業者</th><td>Voice2Content</td></tr>
            <tr><th>運営責任者</th><td>泉</td></tr>
            <tr><th>所在地</th><td>大阪府大阪市西区九条2-13-20-306</td></tr>
            <tr><th>電話番号</th><td070-4222-7450受付時間：平日10:00〜18:00></td></tr>
            <tr><th>メールアドレス</th><td>support@voice2contentai.com</td></tr>
            <tr><th>販売URL</th><td>https://app.voice2contentai.com</td></tr>
            <tr><th>販売価格</th><td>各プランページに表示された金額</td></tr>
            <tr><th>商品内容</th><td>音声→文字起こし＋コンテンツ生成サービス</td></tr>
            <tr><th>支払方法</th><td>クレジットカード（Stripe）</td></tr>
            <tr><th>決済期間</th><td>即時処理</td></tr>
            <tr><th>提供時期</th><td>決済後すぐ</td></tr>
            <tr><th>返品（通常）</th><td>返金不可・解約は次回更新前</td></tr>
            <tr><th>返品（不良）</th><td>不具合時はサポート対応</td></tr>
        </table>
    </div>
    """))

@app.get("/cancel")
def cancel():
    return HTMLResponse(page("キャンセル", "<h1>キャンセル</h1>"))

@app.post("/stripe-webhook")
async def webhook(request: Request):
    return {"status": "ok"}