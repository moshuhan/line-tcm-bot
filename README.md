# LINE TCM AI Bot（中醫課程助教）

以 **Python（Flask）+ OpenAI** 為主的 LINE Bot，專為中醫課程設計。部署於 Vercel，使用 Upstash Redis 儲存狀態，具備時間感知檢索、語音教練、蘇格拉底測驗、主動複習與每週學習報告。

---

## 功能特色

- **LINE Messaging API**：接收／回覆文字、語音、Postback（Rich Menu）。
- **OpenAI**：Assistant API（中醫問答／寫作修訂）、Whisper（語音轉文字）、TTS（示範發音）、GPT-4o-mini（文法、測驗、複習筆記、每週概念標註）。
- **時間感知檢索與課綱**（`config/syllabus.json` + `api/syllabus.py`）：
  - 不鎖定檢索：與中醫／醫療相關問題皆可依知識庫或學術資源回答。
  - 未來課程進度提示：回答後可附加「這是我們第 N 週的重點，你很有先見之明喔！」。
  - 精準過濾：僅對與中醫／醫療學術完全無關的內容回覆「本機器人僅供學業使用」。
- **語音教練**（口說練習模式）：
  - 接收 .m4a → Whisper 轉文字 → 發音準確度（與參考句比對）＋文法檢查（GPT）。
  - 需修正：回饋文字 ＋ OpenAI TTS（shimmer／alloy）示範語音，供影子跟讀。
  - 正確：回覆「太棒了，發音很精準！」＋ Quick Reply「是否要練習其他句子？」（要，再練一句）。
- **蘇格拉底測驗**：
  - 每個中醫問答回覆後附加「要來試試一題小測驗嗎？」Quick Reply【是／否】。
  - 點「否」：回覆「好的，有需要再跟我說～」。
  - 點「是」：依最後一則問題出引導式小題，學生回覆後由 GPT 判斷、鼓勵或修正，並記錄弱項領域。
- **主動複習**：
  - 若某學生在特定領域（經絡、穴位、辨證等）表現不佳達門檻，主動詢問：「發現你對這部分較不熟，需要幫你整理複習筆記嗎？」【要／不要】。
  - 點「要」：產生該領域複習筆記並清除該弱項計數。
- **每週學習報告（Cron）**：
  - 每週五 18:00（台灣時間）執行：彙整所有使用者提問，以 GPT 標註概念後統計「前十大困惑觀念」。
  - 使用 matplotlib 繪製提問次數圖、ReportLab 產出 PDF，經 SMTP 寄至 `REPORT_EMAIL`。

---

## 專案結構

```
.
├── api
│   ├── index.py          # Vercel 入口（Flask）：Webhook、語音、測驗、複習、Cron
│   ├── syllabus.py       # 時間感知檢索與課綱（未來提示、離題過濾、RAG 說明）
│   ├── learning.py       # 問題記錄、蘇格拉底測驗、弱項、複習筆記
│   ├── weekly_report.py  # 每週報告：Redis 取問、概念統計、PDF、SMTP
│   └── webhook.js        # Node 版 Webhook（選用，目前未作主要入口）
├── config
│   └── syllabus.json     # 課綱日期、關鍵字、學業相關關鍵字
├── services/             # Node 用（line / openai / state）
├── scripts/
│   └── setup_rich_menu.js # Rich Menu 設定（Node）
├── docs/
│   └── ARCHITECTURE.md   # 技術架構概覽
├── tests/
├── main.py               # 本地 Flask 執行（精簡版）
├── register_menu.py      # Python 版 Rich Menu 上傳（2500x843）
├── vercel.json           # Rewrite → api/index.py；Cron 每週五 /api/cron/weekly
├── requirements.txt      # Python 依賴（含 reportlab、matplotlib）
├── package.json          # Node 依賴與腳本
├── .env.example          # 環境變數範例
└── README.md
```

---

## 環境變數

複製 `.env.example` 為 `.env` 並填入：

| 變數 | 說明 |
|------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers Console |
| `LINE_CHANNEL_SECRET` | LINE Developers Console |
| `OPENAI_API_KEY` | OpenAI API Key |
| `OPENAI_ASSISTANT_ID` | OpenAI Assistants 建立的助理 ID |
| `KV_REST_API_URL` | Upstash Redis URL |
| `KV_REST_API_TOKEN` | Upstash Redis Token |
| `REPORT_EMAIL` | 每週 PDF 報告寄送信箱（請輸入你的信箱） |
| `CRON_SECRET` | 保護 /api/cron/weekly 的密鑰 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | 寄送每週報告用 SMTP（如 Gmail 應用程式密碼） |

---

## 本地開發

**Python（主要）**

```bash
pip install -r requirements.txt
# 設定 .env 後
python main.py
# 或
python api/index.py  # 若該檔有 if __name__ == "__main__": app.run()
```

**Node（選用）**

```bash
npm install
npm run dev
```

使用 ngrok 等將本機 port 暴露後，在 LINE Developers Console 將 Webhook URL 設為 `https://xxxx.ngrok.io/callback`（Python 入口為 `/callback`）。

---

## Vercel 部署

1. 將本專案推送到 **GitHub**。
2. 登入 [Vercel](https://vercel.com) → **Add New Project** → 匯入 `line-tcm-bot` 倉庫。
3. **Environment Variables** 設定上述所有變數（含 `REPORT_EMAIL`、`CRON_SECRET`、SMTP、Redis）。
4. 部署完成後，記下網域（如 `https://your-project.vercel.app`）。
5. **LINE Developers Console** → Messaging API：
   - **Webhook URL**：`https://your-project.vercel.app/callback`
   - 開啟 **Use webhook**。
6. 若使用 Vercel Cron：在專案設定中確認已啟用 Cron，排程為每週五 10:00 UTC（台灣 18:00）呼叫 `/api/cron/weekly`。需設定 `CRON_SECRET`，Vercel 會以 `Authorization: Bearer <CRON_SECRET>` 呼叫。

---

## 語音教練測試（口說練習）

1. 在 LINE 切換至「口說練習」模式（或說「口說練習」）。
2. 傳送語音訊息（.m4a）；Bot 會回覆辨識結果，並依是否有「當前練習句」：
   - 無：發送新練習句 ＋ TTS 示範（shimmer）。
   - 有：計算發音分數與文法，達標則「太棒了，發音很精準！」＋「是否要練習其他句子？」；未達標則回饋 ＋ 示範 TTS。
3. 點「要，再練一句」可取得下一句並繼續練習。

---

## 蘇格拉底測驗與主動複習

- **測驗**：在中醫問答模式中，每次 AI 回覆後會出現「要來試試一題小測驗嗎？」【是／否】。點「是」會收到一題引導式小題，回覆文字後會得到判斷與鼓勵／修正；答錯會記錄該領域弱項。
- **主動複習**：當某領域弱項次數達門檻且超過冷卻期，Bot 會主動問「需要幫你整理複習筆記嗎？」【要／不要】。點「要」會產出該領域複習筆記並清除該弱項計數。

---

## 每週報告（Cron）

- **自動**：Vercel Cron 每週五 10:00 UTC 呼叫 `GET/POST /api/cron/weekly`（需 `CRON_SECRET`）。
- **手動**：對 `https://你的網域/api/cron/weekly?secret=<CRON_SECRET>` 發 GET 請求（或 Header `Authorization: Bearer <CRON_SECRET>`）。
- 報告會彙整最近 7 天提問、產出前十大困惑觀念、生成 PDF 並寄至 `REPORT_EMAIL`。請在環境變數中設定 **你的信箱** 與 SMTP。

---

## 技術說明

- **入口**：Vercel 將所有請求 rewrite 至 `api/index.py`（Flask）。對話狀態、測驗、弱項、問題記錄皆存於 **Upstash Redis**。
- **架構細節**：見 `docs/ARCHITECTURE.md`。

---

## 授權與注意事項

- 本專案供教學使用；涉及中醫內容之回覆會附加「僅供教學用途，不具醫療建議」聲明。
- 每週報告與 SMTP 寄送依你所填的 `REPORT_EMAIL` 與 SMTP 設定為準，請勿將密碼提交至版控。
