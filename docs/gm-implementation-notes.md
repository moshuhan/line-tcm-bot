# 遊戲化（藥材圖鑑／節氣闖關）實作筆記

對應規格：[tcm-gamification-spec-v2.1.md](tcm-gamification-spec-v2.1.md)。這份是「實際做出來的樣子」與操作方式，規格有出入時以這份為準。

## 1. 檔案地圖

| 檔案 | 作用 |
|---|---|
| `api/gm_core.py` | 核心邏輯：XP／等級／連續天數、掉落、合成、生活線闖關、圖鑑查詢、開發開關（純函式 + `GmService`） |
| `api/gm_content.py` | 讀取 `data/gm/*.json`、題目轉前端版本（不含答案）、後端批改、節氣日期、等級表 |
| `api/gm_store.py` | 使用者資料存取：MongoDB（`gm_users`、`gm_events`），單文件＋樂觀鎖；連不到 Mongo 時退回本機 JSON 備援（只供本機測試） |
| `api/entitlements.py` | 免費／Pro 判斷，**所有 Pro 差異只在這裡** |
| `api/gm_routes.py` | Flask Blueprint：生活線 LIFF、API、靜態圖、開發開關；`GmHooks` 掛接點 |
| `api/gm_share.py` | 分享卡 Flex（`formula_unlocked`、`recipe_unlocked`、`trivia_card`） |
| `api/templates/liff_life.html` | 生活線 LIFF（單頁：首頁／節氣地圖／答題／結果／圖鑑／我的） |
| `assets/gm/gm_rewards.js` | 國考／口說／寫作三頁共用的「遊戲獎勵」區塊 |
| `data/gm/*.json` | 種子資料與設定（`config.json` 是所有數值的來源） |
| `scripts/setup_rich_menu_tabs.js`、`scripts/make_rich_menu_placeholders.py` | Rich menu 兩套選單＋佔位圖 |
| `tests/test_gm_core.py` | 19 項測試：`python -m pytest tests/test_gm_core.py` |

掛接點（都包在 try/except，遊戲化出錯不會影響原功能）：
- 國考：`/api/liff/quiz/submit` 在批改、存 `exam_sessions`、存錯題**之後**呼叫，回應多一個 `gm` 欄位。
- 口說：`/api/liff/speaking/summary`（使用者講滿 `speaking_min_turns` 句才算有效練習）。
- 寫作：`/api/liff/writing/review`（英文單字數達 `writing_min_words` 才算有效練習）。
- 聊天室系統提示：新增「不做診斷」規則（中英文版各一條）。

## 2. 與規格不同、或規格沒寫的決定

1. **國考 XP 與練習 XP 都有每日上限**（`exam_xp_daily_cap` 600、`practice_xp_daily_cap` 120），避免反覆交小考卷或微調同一份文章洗經驗值。
2. **生活線指定掉落不受「每日 7 種新藥材」上限擋住**，只有「當季加成」那張隨機卡受限。否則國考抽卡用完名額後，闖關進度會被卡死。
3. **生活線每題只認第一次作答**（答完立刻鎖定並給解說），避免先看答案再重答。寒熱分類題要「全部分對」才算對。
4. **免費版「每天 1 關」只計「還沒通關的新關卡」**；失敗後重打同一關不額外消耗名額，已通關的關卡重玩不限次數但不重複掉落。
5. **Boss 關保證合成**：Boss 的掉落 = 該季茶飲組成，通關即解鎖茶飲。「不限季節」的茶飲（四神湯、紅棗桂圓茶）與方劑一樣，集齊組成就自動解鎖。
6. **當季加成**：當前節氣那一關首次通關，多掉 1 張隨機生活線食材。免費版是線性解鎖，要玩到那一關才吃得到。
7. **冪等**：同一位使用者、同一組題目與作答，10 分鐘內重送只回傳第一次的結算結果。
8. 口說／寫作：免費版每日第一次有效練習保證掉 1 張；之後只有 Pro 有 50% 機率掉（`extra_practice_rate`）。
9. 方劑的君臣佐使：常見方劑已填入，不確定的（甘草乾薑湯、六君子湯、八珍湯、十全大補湯的部分藥材）標 `TODO: 待內容負責人審核`，介面顯示「角色說明待補」。
10. 圖片：目前只有 5 味藥材（甘草、芍藥、人參、茯苓、白朮）與小銅人 4 張表情。其餘藥材、食材、方劑、茶飲用 emoji 底卡；補圖只要放 `assets/herbs/{herb_id}.webp`（或 `assets/recipes/`、`assets/formulas/`）即可，不需要改程式。

## 3. 本機執行與測試

```bash
# 啟動（連不到 Railway 內網的 Mongo 時，會自動用本機備援儲存）
LINE_CHANNEL_ACCESS_TOKEN=x LINE_CHANNEL_SECRET=y OPENAI_API_KEY=z \
LIFF_DEBUG_TOKEN=<與 liff_*.html 內 DEBUG_TOKEN 相同> GM_DEMO_MODE=1 GM_DEV_SECRET=devsecret \
python -c "import api.index as m; m.app.run(port=5055)"
# 瀏覽器開：http://localhost:5055/liff/life?debug=1
```

`?debug=1` 會用 `LIFF_DEBUG_TOKEN` 跳過 LINE 登入（使用者固定是 `debug-user`）；正式環境不要設 `LIFF_DEBUG_TOKEN`。

## 4. Demo 用開發開關

需同時滿足：環境變數 `GM_DEMO_MODE=1`、密鑰（`GM_DEV_SECRET`，沒設會退回 `CRON_SECRET`）、若設了 `GM_DEV_USER_IDS`（逗號分隔的 LINE userId）則只能操作清單內帳號。正式環境不要開 `GM_DEMO_MODE`，要錄 demo 時才暫時開。

```bash
curl -X POST $BASE/api/gm/dev/<action> -H "X-GM-Dev-Secret: $SECRET" -H "Content-Type: application/json" \
  -d '{"user_id":"U你的測試帳號","params":{...}}'
```

| action | params | 作用 |
|---|---|---|
| `set_pro` | `{"pro":true,"days":30}` | 設為／取消 Pro（days 可省略＝不到期） |
| `open_all` | `{"on":true}` | 24 關全開、不限次數 |
| `demo_drops` | `{"on":true}` | 略過國考題數與 60% 門檻、每日上限，掉落必中 |
| `give` | `{"herb_ids":["gan_cao","shao_yao"]}` | 直接發卡（會觸發自動合成） |
| `set_level` / `add_xp` | `{"level":4}` / `{"xp":500}` | 調整等級／經驗值 |
| `reset_daily` | — | 清今日計數 |
| `reset` | — | 整個帳號重置 |

## 5. 上線前要做的設定

**Railway 環境變數**（沒設也能跑，用預設值）：`PUBLIC_BASE_URL`（分享卡圖片網址；沒設時用請求網址並強制 https）、`GM_LIFE_LIFF_ID`（預設 `2011870208-kD6N9oKn`）。錄 demo 時才加：`GM_DEMO_MODE=1`、`GM_DEV_SECRET`、`GM_DEV_USER_IDS`。

**LINE Developers 後台**：生活線 LIFF（`2011870208-kD6N9oKn`）的 Endpoint URL 是「Railway 網域 + `/liff/life`」，開啟 shareTargetPicker，scope 含 `profile`、`openid`。

**Rich menu**（會把預設選單換成生活版，立刻影響所有使用者）：
```bash
python scripts/make_rich_menu_placeholders.py     # 沒有正式設計圖時先產生佔位圖，之後覆蓋 assets/rich_menu/*.png
node scripts/setup_rich_menu_tabs.js --dry-run    # 先看會做什麼（不呼叫 LINE API）
node scripts/setup_rich_menu_tabs.js              # 正式建立（可重複執行）
```

**MongoDB**：第一次啟動會自動建 `gm_users.user_id` 唯一索引。資料在 `gm_users`（每人一份文件）與 `gm_events`（分享事件）。

## 6. 還沒做（規格的 Phase 2 與之後）

`ref_code`／`Referral`（要先實測 LIFF → 加好友是否保留參數）、`herb_request` 求藥、`season_complete` 分享卡、分享換 AI 額度與額度限制（`entitlements.ai_daily_quota` 已預留、尚未套用到 AI 詳解）、勳章、付費牆與金流、小銅人換裝與動畫、IG／Threads 站外分享、配伍禁忌互動、各藥材食材與方劑茶飲插圖。
