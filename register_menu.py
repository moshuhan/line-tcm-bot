import os
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models.actions import MessageAction
from linebot.models.rich_menu import RichMenu, RichMenuSize, RichMenuArea, RichMenuBounds
import requests

# 1. 讀取 .env 檔案
load_dotenv()

# 2. 從環境變數抓取 Token (請確保你的 .env 裡面是用這個名稱)
TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

if not TOKEN:
    print("錯誤：找不到 LINE_CHANNEL_ACCESS_TOKEN，請檢查 .env 檔案")
    exit()

TOKEN = TOKEN.strip()
line_bot_api = LineBotApi(TOKEN)

# 定義選單結構：四個選項（2500 / 4 = 625 寬 each），點擊後傳送該按鈕文字以確認觸發
# 圖片需為 2500x843，請將底圖存於 assets/rich_menu_background.png
rich_menu_to_create = RichMenu(
    size=RichMenuSize(width=2500, height=843),
    selected=True,
    name="TCM_EMI_Menu",
    chat_bar_text="點我切換學習模式",
    areas=[
        RichMenuArea(
            bounds=RichMenuBounds(x=0, y=0, width=625, height=843),
            action=MessageAction(label="中醫問答", text="中醫問答"),
        ),
        RichMenuArea(
            bounds=RichMenuBounds(x=625, y=0, width=625, height=843),
            action=MessageAction(label="口說練習", text="口說練習"),
        ),
        RichMenuArea(
            bounds=RichMenuBounds(x=1250, y=0, width=625, height=843),
            action=MessageAction(label="寫作修訂", text="寫作修訂"),
        ),
        RichMenuArea(
            bounds=RichMenuBounds(x=1875, y=0, width=625, height=843),
            action=MessageAction(label="課務查詢", text="課務查詢"),
        ),
    ]
)

try:
    # 1. 建立選單架構
    rich_menu_id = line_bot_api.create_rich_menu(rich_menu=rich_menu_to_create)
    print(f"1. 選單已建立，ID: {rich_menu_id}")

    # 2. 使用 Requests 直接上傳圖片 (繞過 SDK 報錯)
    url = f'https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content'
    headers = {
        'Authorization': f'Bearer {TOKEN}',
        'Content-Type': 'image/jpeg' # 如果你的圖是 jpg，請改為 image/jpeg
    }
    
    # 四格選單：中醫問答｜口說練習｜寫作修訂｜課務查詢，請使用 2500x843 底圖
    image_path = "assets/rich_menu_background.png"
    if not os.path.exists(image_path):
        image_path = "assets/rich_menu_background.jpg"
    content_type = "image/png" if image_path.endswith(".png") else "image/jpeg"
    headers["Content-Type"] = content_type
    with open(image_path, 'rb') as f:
        img_data = f.read()

    response = requests.post(url, headers=headers, data=img_data)
    
    if response.status_code == 200:
        print("2. 圖片上傳成功！")
        # 3. 設定為預設選單
        line_bot_api.set_default_rich_menu(rich_menu_id)
        print("3. 已成功設為預設選單！")
    else:
        print(f"2. 圖片上傳失敗，狀態碼: {response.status_code}")
        print(f"錯誤訊息: {response.text}")

except Exception as e:
    print(f"發生非預期錯誤: {e}")


