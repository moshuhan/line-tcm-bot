import os
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage
from upstash_redis import Redis
from openai import OpenAI

app = Flask(__name__)
app.debug = True # 選配：方便看更多詳細錯誤
# 新增這行，明確指定給 Vercel
app = app

# 1. 初始化所有連線資訊 (金鑰會自動從 Vercel 環境變數讀取)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
redis = Redis(url=os.getenv("KV_REST_API_URL"), token=os.getenv("KV_REST_API_TOKEN"))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

# 2. LINE Webhook 進入點
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    
    # 在這裡只做最基本的驗證，然後快速回傳 OK
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK', 200 # 務必確保這裡快速回傳 200

# 3. 處理模式切換 (Postback)
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    # 取得 data (例如 'mode=speaking')
    mode = event.postback.data.split('=')[1] if '=' in event.postback.data else "tcm"
    
    # 將狀態存入 Vercel KV
    redis.set(f"user_mode:{user_id}", mode)
    
    # 模式名稱對照
    mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}
    reply_msg = f"已切換至【{mode_map.get(mode, '未知')}】模式，請開始輸入！"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))

# 4. 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    
    # A. 取得模式 (單純為了在回覆中顯示)
    mode_raw = redis.get(f"user_mode:{user_id}")
    mode = mode_raw.decode('utf-8') if mode_raw else "tcm"
    mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}

    # B. 立即回覆，防止 LINE Webhook 超時
    line_bot_api.reply_message(
        event.reply_token, 
        TextSendMessage(text=f"已收到您的訊息，正在以【{mode_map.get(mode, '中醫專家')}】模式分析中...")
    )
    
    # C. 呼叫後台 AI 處理 (內部會用 push_message 回傳答案)
    process_ai_request(event, user_id, user_text)

# 5. 處理語音訊息
@handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    user_id = event.source.user_id
    message_id = event.message.id
    
    # A. 立即回覆，防止 LINE Webhook 超時
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="🎙️ 收到語音！正在轉換並分析中，請稍候...")
    )
    
    # B. 下載語音檔到 Vercel 的暫存空間
    message_content = line_bot_api.get_message_content(message_id)
    temp_path = f"/tmp/{message_id}.m4a"
    with open(temp_path, 'wb') as f:
        for chunk in message_content.iter_content():
            f.write(chunk)
    
    try:
        # C. 語音轉文字
        with open(temp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )
        user_voice_text = transcript.text
        os.remove(temp_path) # 刪除暫存
        
        # D. 告知辨識結果 (用 push)
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🎤 辨識內容：\n「{user_voice_text}」"))
        
        # E. 串接 AI 處理 (用 push)
        process_ai_request(event, user_id, user_voice_text, is_voice=True)

    except Exception as e:
        print(f"語音處理出錯: {e}")
        line_bot_api.push_message(user_id, TextSendMessage(text="❌ 語音辨識失敗，請確認錄音品質後再試一次。"))

# 6. 整合 AI 處理邏輯 (統一處理文字與語音轉出的文字)
def process_ai_request(event, user_id, text, is_voice=False):
    # --- A. 決定模式標籤 ---
    # 從 Redis 讀取模式，記得將 bytes 轉為 string
    mode_raw = redis.get(f"user_mode:{user_id}")
    mode = mode_raw.decode('utf-8') if mode_raw else "tcm"

    tag = "[中醫專家模式]"
    if mode == "speaking": tag = "[口說教練模式]"
    elif mode == "writing": tag = "[寫作顧問模式]"

    # --- B. 管理 Thread ID (對話記憶) ---
    # 從 Redis 讀取該使用者的專屬 Thread ID
    thread_id_raw = redis.get(f"user_thread:{user_id}")
    thread_id = thread_id_raw.decode('utf-8') if thread_id_raw else None
    
    if not thread_id:
        # 如果是新朋友，建立新 Thread 並存入 Redis
        thread = client.beta.threads.create()
        thread_id = thread.id
        redis.set(f"user_thread:{user_id}", thread_id)
    
    # --- C. 傳送訊息給 OpenAI Assistant ---
    # 組合內容：強制命令 AI 切換身分 + 使用者訊息
    full_content = f"【請切換至以下身分：{tag}】\n\n學生的訊息內容如下：{text}"
    
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=full_content
    )
    
    # --- D. 執行 Run 並等待回覆 ---
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id
    )
    
    # 輪詢檢查狀態
    while run.status in ['queued', 'in_progress']:
        time.sleep(1)
        run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
    
    # 取得結果並回傳
    if run.status == 'completed':
        messages = client.beta.threads.messages.list(thread_id=thread_id)
        ai_reply = messages.data[0].content[0].text.value
        # 使用 push_message 避免 LINE Webhook 超時
        line_bot_api.push_message(user_id, TextSendMessage(text=ai_reply))
    else:
        line_bot_api.push_message(user_id, TextSendMessage(text="抱歉，AI 思考太久了，請再試一次！"))
    
    # 根據模式決定標籤
    tag = "[中醫專家模式]"
    if mode == "speaking": tag = "[口說教練模式]"
    elif mode == "writing": tag = "[寫作顧問模式]"
    
    # 1. 建立 Thread (為了簡化，每次都建新的或抓舊的，這裡先示範建新的)
    thread = client.beta.threads.create()
    
    # 2. 傳送訊息
    client.beta.threads.messages.create(
    thread_id=thread.id,
    role="user",
    content=f"【請切換至以下身分：{tag}】\n\n學生的訊息內容如下：{text}"
)
    
    # 3. 執行 Run
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant_id
    )
    
    # 4. 等待結果 (Vercel 有時間限制，這裡用簡單的輪詢)
    while run.status in ['queued', 'in_progress']:
        time.sleep(1)
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
    
    # 5. 取得回答並回傳
    if run.status == 'completed':
        messages = client.beta.threads.messages.list(thread_id=thread.id)
        ai_reply = messages.data[0].content[0].text.value
        line_bot_api.push_message(user_id, TextSendMessage(text=ai_reply))




