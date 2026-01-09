import os
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage
from upstash_redis import Redis
from openai import OpenAI

app = Flask(__name__)

# 1. 初始化所有連線資訊 (金鑰會自動從 Vercel 環境變數讀取)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
redis = Redis(url=os.getenv("KV_REST_API_URL"), token=os.getenv("KV_REST_API_TOKEN"))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

# 2. LINE Webhook 進入點
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

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

# 4. 處理文字訊息 (依據模式呼叫 AI)
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    process_ai_request(event, user_id, user_text)
    
    # 從 Redis 讀取該使用者目前的模式 (預設為 tcm)
    mode = redis.get(f"user_mode:{user_id}") or "tcm"
    
    # 根據模式決定傳給 AI 的指令前綴 (System Instruction)
    prompts = {
        "tcm": "你是中醫專家，請針對以下問題提供專業建議：",
        "speaking": "你是 EMI 英文口說教練，請分析以下句子的發音重點與醫學術語：",
        "writing": "你是學術寫作顧問，請針對以下段落提供 Grammar, Terminology, Logic 三方面的修訂建議："
    }
    system_prefix = prompts.get(mode, prompts["tcm"])

    # 這裡請接上你原本的 OpenAI Assistant 呼叫邏輯 (例如建立 Thread 並送出訊息)
    # 範例回覆：
    line_bot_api.reply_message(
        event.reply_token, 
        TextSendMessage(text=f"（模式：{mode}）正在處理您的請求...")
    )
# 5. 新增：處理語音訊息
@handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    user_id = event.source.user_id
    message_id = event.message.id
    
    # 從 LINE 伺服器下載語音檔案
    message_content = line_bot_api.get_message_content(message_id)
    temp_path = f"/tmp/{message_id}.m4a"
    with open(temp_path, 'wb') as f:
        for chunk in message_content.iter_content():
            f.write(chunk)
    
    # 呼叫 OpenAI Whisper 將語音轉文字
    with open(temp_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1", 
            file=audio_file
        )
    
    # 刪除暫存檔
    os.remove(temp_path)
    
    # 轉出的文字內容
    user_voice_text = transcript.text
    
    # 告訴使用者聽到了什麼，並開始處理
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"🎤 我聽到您說：\n「{user_voice_text}」\n正在分析中...")
    )
    
    # 接下來同樣丟給 AI 邏輯處理 (帶入標籤)
    process_ai_request(event, user_id, user_voice_text, is_voice=True)

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

if __name__ == "__main__":
    app.run()



   


