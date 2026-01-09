import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, PostbackEvent
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

if __name__ == "__main__":
    app.run()