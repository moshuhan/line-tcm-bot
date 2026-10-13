# 岐黃 AI 遊戲化 — 規格文件 v2.1（學生線 + 生活線）

> 給 Claude Code 的實作說明。本版取代 v2。
> 請先閱讀現有 repo 的架構（LINE Bot 後端、RAG、OpenAI API 的使用方式、以 LINE userId 管理 session、資料庫、現有三個 LIFF），再依本文件規劃實作。與現有架構衝突時，以現有架構為準並回報。
> **本階段的首要目標是做出能拍 demo 影片的初步版本**。題目不必全部上線，能跑通完整流程優先。
> 第 11 節是任務分工，第 12 節是 demo 腳本（驗收目標）。

## 0. v2 → v2.1 變更摘要

1. **國考掉落簡化**：交卷批改、寫入紀錄與錯題本之後再結算；題數達門檻且得分 ≥ 60% 才抽 3 張卡（可重複）。取消 Combo 加成與保底機制。
2. **口說、寫作掉落**：加最低完成條件；掉落一律由後端抽籤。
3. **高手區解鎖條件**：拿掉「配伍禁忌考點熟練度」，只看等級。配伍禁忌教學事件移到「之後」階段。
4. **生活線免費版**：由「節氣日期開放」改為**線性解鎖**，節氣只作為加成。
5. **生活線不提供自由問答入口**：選單不放「問小銅人」。
6. **預設 rich menu 為生活版**。
7. **新增 demo 用開發開關**（第 7.4 節）與 **demo 腳本**（第 12 節）。
8. **階段重排**：rich menu 與基本分享提前到 Phase 1，因為 demo 需要。
9. 移除每日一題與每日簽到相關設計（若程式中有殘留請一併不實作）。

---

## 1. 背景與定位

- 現有產品：LINE 上的中醫教育聊天機器人（RAG）。已有三個 LIFF（學生用：國考問答、英文口說、英文寫作）。
- 本版把受眾從「中醫系學生」擴大為兩條線，共用一套系統：
  - **學生線**：國考刷題 + 英文口說寫作 → 收集藥材 → 合成**方劑**
  - **生活線**：一般民眾，節氣闖關答題 → 收集藥食同源食材 → 合成**茶飲、藥膳**
- 兩線共用：藥材圖鑑（同一套藥材資料）、吉祥物小銅人、分享機制、**單一 Pro 方案**。
- 產品主軸：讓人每天願意回來。遊戲化負責留存，Pro 負責變現。

---

## 2. 共用核心

### 2.1 藥材圖鑑（兩線共用）

- 一套 `Herb` 資料，兩條線都從同一張表掉落。民眾在生活線拿到的茯苓、大棗、生薑，在學生線就是四君子湯、桂枝湯的材料。
- 每味藥材標記 `tracks[]`（`student` / `life`）。藥食同源食材兩線都可掉落。
- 卡片資訊：中英文名稱、拉丁名、性味歸經、功效概述（教科書層級、中英雙語）、《神農本草經》三品（僅作知識欄位）、插圖。

### 2.2 吉祥物：小銅人

- 原型：王惟一主持鑄造的天聖針灸銅人。肚子是藥櫃（圖鑑入口）。
- **第一版只放靜態圖，不做動畫**：4 張去背圖 `assets/mascot/{state}.webp`，state = `happy`（答對）、`ouch`（答錯）、`thinking`（載入）、`celebrate`（合成）。
- 元件需支援之後依服裝換圖：`assets/mascot/{outfit}/{state}.webp`。
- 出現在：兩線的答題頁、圖鑑頁、合成結果頁、分享卡。

### 2.3 等級與連續天數

- 一個使用者一套 XP / 等級 / 連續天數，兩線活動都累積到同一帳號。
- 連續天數：當天在任一線完成一次練習或一關即累計。

---

## 3. 學生線

### 3.1 等級與解鎖區

解鎖區依現代毒性與藥性峻烈程度劃分，**不從三品推導**，由內容負責人逐味指定（見 3.5 表格的「解鎖區」欄）。三品只當卡片上的知識欄位。

| 解鎖區 | 開放等級 | 藥材 |
|---|---|---|
| 新手區 | Lv1 | 其餘常用、無毒藥材 |
| 進階區 | Lv4 | 麻黃、杏仁、厚朴、枳實 |
| 高手區 | Lv7 | 附子、半夏、大黃 |

等級表（數值放設定檔）：

| Lv | 名稱 | 累積 XP |
|---|---|---|
| 1 | 採藥童子 | 0 |
| 2 | 抓藥學徒 | 300 |
| 3 | 藥櫃夥計 | 800 |
| 4 | 坐堂學徒 | 1,800 |
| 5 | 鈴醫 | 3,000 |
| 6 | 郎中 | 4,500 |
| 7 | 坐堂醫 | 6,500 |
| 8 | 名醫 | 10,000 |
| 9 | 御醫 | 15,000 |
| 10 | 國醫 | 22,000 |

XP：答對 +10、答錯 +2、口說 / 寫作每次完成 +30、連續天數每日 +20、生活線過關 +20。**XP 與連續天數不受 60% 門檻影響**，交卷即給。

### 3.2 掉落機制（數值全部放設定檔）

**掉落結算一律在後端執行，前端只負責顯示結果。**

#### 國考題區（交卷結算）

- 掉落掛在現有交卷流程之後：**批改 → 寫入 `exam_sessions` → 寫入錯題本 → 結算掉落**。掉落結果放進交卷 API 的回傳值（例如 `drops` 欄位），結果頁依序翻出卡片。
- 條件：**這份考卷題數 ≥ `EXAM_DROP_MIN_QUESTIONS`（預設 10）且得分 ≥ `EXAM_DROP_MIN_SCORE`（預設 60%）**，才進入抽卡。
- 達標時抽 `EXAM_DROP_DRAWS` 張（預設 3），**獨立抽取、可以重複**。
- 防刷：
  - 每位使用者每天最多 `EXAM_DROP_SUBMISSIONS_PER_DAY`（預設 3）次可抽卡的交卷。
  - **同一份考卷（同一個 session）只結算一次**，需具冪等性，重送請求不會再掉。
  - 未作答視為答錯（沿用現有 `submit_exam` 行為）。
- **首次交卷必掉甘草**：使用者的第一次交卷不受題數與 60% 門檻限制，保證掉落甘草（新手引導）。其後正常適用門檻。
- 沒有 Combo 加成，沒有保底機制。

#### 口說區、寫作區

- 免費版每日那一次練習**保證掉落 1 張**；Pro 額外練習每次 `EXTRA_PRACTICE_DROP_RATE`（預設 50%）。
- **最低完成條件**才算一次有效練習並掉落：口說至少 `SPEAKING_MIN_TURNS` 輪對話（預設 5）、寫作至少 `WRITING_MIN_WORDS` 字詞（預設 50）。預設值待成本表確定後調整。
- 掛接點（請依實際程式確認）：口說在 `/api/liff/speaking/summary` 完成處；寫作在 `/api/liff/writing/save` 或 `/api/liff/writing/review` 完成處。
- 同一個練習 session 只結算一次。

#### 抽哪一味

- 從「該模組藥材池（3.3 表）∩ 已解鎖區」加權隨機。
- 最常用 5 味（甘草、芍藥、人參、茯苓、白朮）權重 3，其餘 1；**只差一味就能合成的方劑所需藥材權重 ×2**。
- **每日上限**：每天最多獲得 `DAILY_DISTINCT_HERB_CAP`（預設 7）種**不同的新藥材**（兩線合計）。達上限後，抽卡只會落在已擁有的藥材（重複卡）。

### 3.3 模組專屬掉落

| 方劑 | 國考題區 | 口說區 | 寫作區 |
|---|---|---|---|
| 芍藥甘草湯 / 桔梗湯 / 桂枝甘草湯 | 全部 | | |
| 四君子湯 | 人參 | 茯苓 | 白朮 |
| 四物湯 | 當歸、白芍 | 川芎 | 熟地黃 |
| 桂枝湯 | 桂枝、芍藥 | 生薑 | 大棗 |
| 麻黃湯 | 麻黃、桂枝 | 杏仁 | |
| 六君子湯（四君子 +） | | 陳皮 | 半夏 |
| 十全大補湯（八珍 +） | | 黃耆 | 肉桂 |
| 四逆湯 | 附子 | | 乾薑 |
| 小承氣湯 | 大黃 | 厚朴 | 枳實 |

甘草三區皆掉落。

### 3.4 方劑合成

- 持有全部組成即**自動解鎖**方劑；解鎖畫面說明君臣佐使。
- 方劑卡只呈現組成、出處、教科書層級功效概述，**不顯示劑量**。
- 方劑進化鏈：四君子湯 → 六君子湯；四君子湯 + 四物湯 → 八珍湯 → 十全大補湯；桂枝甘草湯 → 桂枝湯。
- 配伍禁忌（十八反、十九畏）的互動教學：**本階段不做**，列入「之後」。資料表 `IncompatiblePair` 可先建好但不實作互動。

### 3.5 學生線種子藥材

| 藥材 | 三品 | 解鎖區 | 掉落模組 | 權重 | 生活線也掉落 |
|---|---|---|---|---|---|
| 甘草 | 上品 | 新手 | 全區 | 3 | |
| 芍藥（白芍） | 中品 | 新手 | 國考 | 3 | |
| 人參 | 上品 | 新手 | 國考 | 3 | |
| 茯苓 | 上品 | 新手 | 口說 | 3 | ✓ |
| 白朮 | 上品 | 新手 | 寫作 | 3 | |
| 桂枝 | 上品 | 新手 | 國考 | 1 | |
| 當歸 | 中品 | 新手 | 國考 | 1 | |
| 桔梗 | 下品 | 新手 | 國考 | 1 | |
| 麻黃 | 中品 | 進階 | 國考 | 1 | |
| 大黃 | 下品 | 高手 | 國考 | 1 | |
| 附子 | 下品 | 高手 | 國考 | 1 | |
| 川芎 | 上品 | 新手 | 口說 | 1 | |
| 陳皮 | 上品 | 新手 | 口說 | 1 | |
| 黃耆 | 上品 | 新手 | 口說 | 1 | |
| 生薑 | 中品 | 新手 | 口說 | 1 | ✓ |
| 厚朴 | 中品 | 進階 | 口說 | 1 | |
| 杏仁 | 下品 | 進階 | 口說 | 1 | |
| 熟地黃 | 上品 | 新手 | 寫作 | 1 | |
| 大棗 | 上品 | 新手 | 寫作 | 1 | ✓ |
| 肉桂 | 上品 | 新手 | 寫作 | 1 | |
| 乾薑 | 中品 | 新手 | 寫作 | 1 | |
| 枳實 | 中品 | 進階 | 寫作 | 1 | |
| 半夏 | 下品 | 高手 | 寫作 | 1 | |

> 三品欄位上線前需內容負責人校對（例如熟地黃為炮製品，《神農本草經》載的是乾地黃）。

---

## 4. 生活線

### 4.1 核心循環

**節氣闖關 → 掉落食材 → 合成茶飲／藥膳 → 分享**。只有「答題」一種互動，全部在同一個 LIFF 內完成。

### 4.2 關卡地圖：四季 × 二十四節氣

- 四章（春、夏、秋、冬），每章 6 關，對應 6 個節氣，共 24 關。
- 每章第 6 關為 Boss 關，過關後合成該季代表茶飲或藥膳。
- 一般關：3 題，答對 ≥ 2 題過關。Boss 關：5 題，答對 ≥ 4 題過關。
- 過關掉落該關指定的 1–2 張食材卡（受每日不同藥材上限約束）。
- **開放規則（v2.1 修訂）**：
  - **免費：線性解鎖**。依節氣順序（立春 → … → 大寒），前一關過關後下一關開放；**每天最多可玩 1 關**。Boss 關需先通過該章前 5 關。已過關的關卡可以重玩，重玩不重複掉落。
  - **Pro：24 關全部隨時開放，每天不限關數。**
  - 未開放的關卡在地圖上反灰。
- **節氣加成**：建立 2026–2027 年的節氣日期表（設定檔）。當前所處節氣所對應的那一關，過關時**多掉 1 張**食材卡，並在地圖上標示「當季」。其餘關卡不受日期限制。
- 為方便 demo，開發開關可一鍵開放全部關卡（第 7.4 節）。

### 4.3 題型（四種）

| 題型 | 互動方式 | 範例 |
|---|---|---|
| `hot_cold_sort` 寒熱分類 | 點選或拖曳，把食材放進「寒涼」「溫熱」兩區 | 西瓜、薑、綠豆、羊肉 |
| `myth_true_false` 迷思真假 | 是／否 | 「冬天進補越多越好」→ 錯 |
| `identify_image` 看圖認食材 | 圖片 + 選擇題 | 這是紅棗、枸杞還是山楂？ |
| `trivia_choice` 冷知識選擇題 | 選擇題 | 「杏林」為什麼代表醫界？ |

- **每題答完都要顯示解說卡**（小銅人表情 + 一段解說）。冷知識與迷思題的解說卡可直接分享（見第 6 節）。
- 民間傳說類內容，解說卡用「相傳」「常見說法」字樣，不寫成史實。
- 若時間不足，優先順序：`trivia_choice` ＞ `myth_true_false` ＞ `identify_image` ＞ `hot_cold_sort`（拖曳最重）。

### 4.4 冷知識種子題（內容已由 Momo 看過，可再調整）

| 題目 | 正解 | 解說 |
|---|---|---|
| 「杏林」為什麼代表醫界？ | 醫生請病人種杏樹 | 相傳三國時的醫生董奉看病不收錢，只請病癒的人種杏樹，多年後成了一片杏林。 |
| 「懸壺濟世」的壺是什麼？ | 賣藥老翁掛的葫蘆 | 典故來自賣藥老翁「壺公」，他在店門口掛著一個葫蘆。 |
| 《神農本草經》收了幾種藥？ | 365 種 | 剛好對應一年的天數。 |
| 四神湯為什麼叫「四神」？ | 由「四臣」諧音而來 | 常見說法是原本叫「四臣湯」，台語發音相近，傳著傳著就變成了四神。 |
| 宋代的針灸銅人有什麼用途？ | 考試用的教具 | 相傳用來考學生扎穴位，這也是小銅人的身世。 |
| 端午節的雄黃酒能喝嗎？ | 不建議 | 雄黃含砷，有毒，現代不建議飲用。 |

### 4.5 生活線食材與茶飲／藥膳（種子資料）

**食材**（`tracks` 含 `life`）：紅棗（即大棗）、生薑、茯苓、枸杞、菊花、山藥、蓮子、芡實、龍眼肉、綠豆、白木耳。

**茶飲／藥膳**（`Recipe`，與學生線 `Formula` 分開）：

| 季節 | Boss 合成 | 組成 |
|---|---|---|
| 春 | 枸杞菊花茶 | 枸杞、菊花 |
| 夏 | 綠豆湯 | 綠豆 |
| 秋 | 銀耳蓮子湯 | 白木耳、蓮子 |
| 冬 | 薑棗茶 | 生薑、紅棗 |
| 不限季節 | 四神湯 | 茯苓、山藥、蓮子、芡實 |
| 不限季節 | 紅棗桂圓茶 | 紅棗、龍眼肉 |

- 茶飲／藥膳卡呈現：組成、傳統用法簡述、注意事項。**完整食譜做法為 Pro 內容**。
- 每張卡固定附註：「傳統飲食文化內容，非醫療建議。孕婦、慢性病或正在服藥者，請先諮詢醫師。」
- 文案**不得宣稱療效**（不可寫「治失眠」「降血壓」等）。

### 4.6 每關資料結構

每關指定：節氣、季節、順序、題目 id 清單、過關掉落的食材 id、是否為 Boss 關、Boss 關合成的 recipe id。關卡設定放 JSON seed 檔，方便之後替換題目。**24 關盡量全部放入**，題目不足的關卡可放範例題並標示 `TODO: 待補`。

---

## 5. Rich menu：分頁切換

- 兩套 rich menu：`student`、`life`，使用 rich menu alias 與 `richmenuswitch` action，選單上方兩個分頁「學生」「生活」，點擊切換。
- **生活版選單**（預設）：節氣闖關、食材圖鑑、今日節氣、我的進度（等級、連續天數）。**不放「問小銅人」**。
- **學生版選單**：國考刷題、英文口說、英文寫作（現有三個 LIFF）、藥材圖鑑。
- 藥材圖鑑兩邊共用同一頁（依 `track` 參數預設篩選）。
- **不做身分詢問、不做 per-user 選單綁定**。所有使用者看到同一套預設選單（生活版），自行用分頁切換。
- `UserProgress.primary_track` 依近 30 天哪一線的活動次數較多**自動判斷**，不需使用者填寫，用於後台統計兩線使用者比例。
- 建立選單的流程寫成可重複執行的腳本（建立 menu、上傳圖片、建立 alias、設定預設選單），方便之後替換圖片。可參考 repo 內既有的 `scripts/setup_rich_menu.js`。
- 選單圖片：先由程式產生 2500×1686 的佔位圖（文字標示各按鈕區塊），等 Momo 提供正式設計後替換。

---

## 6. 分享

- LINE 內：Flex Message（Bubble）+ LIFF `shareTargetPicker`，需在 LINE Developers 開啟 shareTargetPicker。**shareTargetPicker 只能在 LINE App 內的 LIFF 運作**，桌面瀏覽器不會呈現；開發時請預留 `isApiAvailable('shareTargetPicker')` 的 fallback（複製連結）。
- 分享卡只出現在這些時刻：

| share_type | 線 | 時刻 | 階段 |
|---|---|---|---|
| `formula_unlocked` | 學生 | 合成新方劑 | Phase 1 |
| `recipe_unlocked` | 生活 | 合成茶飲／藥膳 | Phase 1 |
| `trivia_card` | 生活 | 冷知識、迷思題的解說卡 | Phase 1 |
| `season_complete` | 生活 | 完成一季的節氣地圖 | Phase 2 |
| `herb_request` | 學生 | 求藥（「只差一味甘草就能組出四君子湯」） | Phase 2 |

- Footer 按鈕：「我也要玩」→ 指向加好友的入口。
- **推薦碼（`ref_code`）列為 Phase 2**。已知風險：一般官方帳號的加好友連結與 QR code 不一定能攜帶自訂參數，需先實測「帶參數的 LIFF 網址 → 加好友 → 保留 `ref_code`」是否可行（見第 12 節待確認）。實測前，Phase 1 的按鈕只需連到加好友頁。
- 分享成功可換當日額外 AI 額度（每日上限放設定檔）。
- IG / Threads 站外分享列為之後階段。

---

## 7. Pro 方案

### 7.1 原則

- **只有一個 Pro。付費後同時解鎖學生線與生活線的全部 Pro 功能**，不分版本。
- 遊戲化核心全部免費。
- **藥材與食材絕不付費取得**：不賣卡、不賣卡包、不做付費抽卡。Pro 解鎖的是「更多可以玩的關卡與練習次數」，卡片仍需自己玩出來。
- Pro 加值只能是內容、次數、外觀或便利類。

### 7.2 功能切分

| 功能 | 免費 | Pro |
|---|---|---|
| 題庫作答、等級、圖鑑、合成、分享 | 完整 | 完整 |
| 學生線 AI 詳解與問答 | 每日 N 次 + 分享獎勵 | 無限 |
| 學生線 口說、寫作 | 每日各 1 次短練習（保證掉落） | 無限、完整長度（額外練習 50% 掉落） |
| 學生線 錯題本 | 基本 | 依考點篩選、自動複習 |
| 學生線 及格缺口分析 | 簡易版 | 完整 |
| 生活線 節氣關卡 | 線性解鎖，每天 1 關 | 24 關隨時玩，每天不限 |
| 生活線 茶飲／藥膳卡 | 傳統用法 | 完整食譜做法與每週採買清單 |
| 小銅人外觀 | 基本造型 | 季節限定造型、特殊分享卡面 |

> v2 的「生活線 AI 養生問答」從 Pro 清單移除（本階段不提供生活線自由問答入口）。

### 7.3 實作要求

- 所有付費判斷集中在單一 `entitlements` 模組，以 `UserProgress.plan` 判斷，不要把 `if pro` 散落各處。
- N、每日額度、口說秒數 / 寫作字數上限、Pro 定價全部放設定檔，不寫死。
- 第一階段只預留結構，不串金流。
- 額度或每日關卡用完時，提示同時提供「升級 Pro」與「分享給朋友換額度」兩個選項。

### 7.4 開發開關（供 demo 與測試）

- 以環境變數（例如 `DEMO_MODE=1`）加**測試帳號白名單**（LINE userId 清單）控制；**非白名單帳號與正式環境不得觸發**。
- 提供腳本或受保護的管理端點，功能包括：
  1. 把指定使用者設為 / 取消 Pro。
  2. 一鍵開放全部 24 關。
  3. 重置指定使用者的進度、圖鑑、每日計數。
  4. Demo 模式：掉落機率 100%、略過國考的題數與 60% 門檻、略過每日上限。
  5. 把指定藥材 / 食材直接發給指定使用者（為了 demo 能快速湊齊一個方劑）。
- 這些端點需驗證密鑰（可沿用現有 `CRON_SECRET` 的檢查模式）。

---

## 8. 資料模型（新增與修改，依現有 DB 調整）

```
Herb
  id, name_zh, name_pinyin, name_en, name_latin
  nature (四氣), flavor (五味), meridians (歸經[])
  function_summary_zh, function_summary_en    # 教科書層級簡述
  shennong_grade            # upper | middle | lower，僅供知識顯示
  unlock_tier               # beginner | intermediate | advanced，內容負責人逐味指定
  tracks[]                  # student | life
  drop_modules[]            # exam | speaking | writing（學生線用）
  drop_weight
  image_url
  is_toxic, toxicity_note

Formula
  id, name_zh, name_en, source (出處，如《傷寒論》)
  category, function_summary_zh/en, image_url

FormulaComponent
  formula_id, herb_id, role (jun|chen|zuo|shi), role_explanation

FormulaEvolution
  from_formula_id, to_formula_id, added_herb_ids[], type (modification|combination)

IncompatiblePair             # 先建表，本階段不做互動
  herb_a_id, herb_b_id, type (eighteen_antagonisms|nineteen_incompatibilities), explanation

Recipe                       # 生活線茶飲／藥膳，與 Formula 分開
  id, name_zh, name_en, season (spring|summer|autumn|winter|any)
  traditional_use_zh/en, caution_zh/en
  full_recipe_zh/en         # Pro 內容
  image_url

RecipeComponent
  recipe_id, herb_id

SolarTerm
  id, name_zh, name_en, season, date (per year), order (1–24)

LifeLevel
  id, solar_term_id, order, is_boss
  question_ids[], drop_herb_ids[]
  unlock_recipe_id          # Boss 關

LifeQuestion
  id, type                  # hot_cold_sort | myth_true_false | identify_image | trivia_choice
  prompt_zh, prompt_en
  payload                   # 依題型：選項、正解、分類答案、圖片
  explanation_zh, explanation_en
  is_shareable, status      # status: ok | todo

UserLifeProgress
  line_user_id, level_id, cleared_at, best_score

UserHerb
  line_user_id, herb_id, quantity, first_obtained_at, source (exam|speaking|writing|life|gift|referral|admin)

UserFormula / UserRecipe
  line_user_id, formula_id|recipe_id, unlocked_at

UserProgress
  line_user_id, xp, level
  streak_days, last_practice_date
  plan                      # free | pro
  pro_expires_at
  primary_track             # student | life，依近期使用紀錄自動判斷
  first_exam_submitted      # 首次交卷必掉甘草用
  exam_drop_submissions_today, exam_drop_counter_date
  life_levels_played_today, life_counter_date
  new_herbs_today, new_herbs_counter_date     # 每日不同新藥材上限

DropSettlement               # 冪等性與稽核
  id, line_user_id, source (exam|speaking|writing|life), source_ref_id（如 exam_sessions 的 _id）
  drawn_herb_ids[], settled_at
  # (source, source_ref_id) 需唯一，避免重複結算

ShareEvent
  id, line_user_id, share_type, target_platform, ref_code, created_at

Referral                     # Phase 2
  ref_code, inviter_line_user_id, invitee_line_user_id, created_at, rewarded

AIQuota
  line_user_id, date, base_quota, bonus_quota, used

Badge / UserBadge
  badge_id, name, condition, is_pro_exclusive
  line_user_id, badge_id, earned_at

HerbRequest                  # Phase 2
  id, requester_line_user_id, herb_id, target_formula_id
  status (open|fulfilled|expired), fulfilled_by_line_user_id, created_at, expires_at
```

> 沒有 `ConceptMastery`、`ConceptHerbMap`：v2.1 不做考點熟練度與考點掉落對應。

---

## 9. 隱私與安全規則（必須遵守）

1. 分享卡只包含遊戲成就與知識內容，絕不帶出對話內容、症狀或個人健康資訊。
2. 方劑卡不顯示劑量；茶飲／藥膳卡與方劑頁都要附「非醫療建議」聲明。
3. **不得宣稱療效**，也不得暗示上品「最補」、下品「較弱」；稀有度（權重）與藥效好壞無關。
4. **AI 不做診斷**：現有聊天室對所有文字訊息都會回覆中醫問答。雖然生活版選單不提供問答入口，使用者仍可直接輸入文字，因此**必須在現有聊天室的系統提示中加入規則**：遇到症狀描述時，回覆以一般知識為限並建議就醫，不推薦具體茶飲或藥材給特定症狀。
5. 民間傳說類內容標示「相傳」「常見說法」。
6. 保育類或已禁用的動物藥不列入可收集獎勵。
7. 分享卡上的暱稱需可選擇隱藏。
8. 開發開關（7.4）不得在正式環境對一般使用者開放。

---

## 10. 實作階段

| 階段 | 內容 |
|---|---|
| 1（demo MVP） | 資料模型與種子資料、`entitlements` 與開發開關、**掉落模組（獨立模組，國考／口說／寫作／生活線共用）**、國考交卷後掉落與結果頁翻牌、方劑合成、生活線地圖與題型、Recipe 合成、圖鑑頁、小銅人元件（佔位圖）、rich menu 分頁切換（預設生活版）、基本分享（`formula_unlocked`、`recipe_unlocked`、`trivia_card`） |
| 2 | `ref_code` / `Referral`（先做 LIFF 實測）、`herb_request`、`season_complete`、分享換額度、額度限制與付費牆提示、連續天數與勳章完善 |
| 之後 | IG／Threads 站外分享、等級換裝、動畫、隱藏方劑、藥材交換、配伍禁忌互動教學、生活線自由問答 |

### Phase 1 建議實作順序（以 demo 為導向，逐條打通）

1. 共用骨架：資料模型、種子資料載入、`entitlements`、開發開關、掉落模組。
2. **生活線垂直切片**：節氣地圖 → 答題 → 解說卡 → 掉落食材 → 合成茶飲（例如薑棗茶）→ 分享。
3. **學生線國考掉落**：交卷 → 結算 → 翻牌 → 合成芍藥甘草湯。
4. 口說、寫作掉落（掛在完成點）。
5. Rich menu 分頁與預設選單。
6. 圖鑑頁、等級與連續天數顯示。
7. 補齊其餘題型與 24 關內容。

---

## 11. 任務分工

### 現在就做（Claude Code）

- 第 8 節的資料模型與 migration。
- 種子資料 JSON：學生線藥材與方劑、生活線食材、茶飲／藥膳、24 節氣（2026–2027 日期）、24 關卡設定、第 4.4 節冷知識題；其餘題目先放範例題並標示 `TODO: 待補`。
- 掉落模組（含冪等結算、每日上限、首次交卷必掉甘草）。
- 在現有交卷流程（`submit_exam` 之後）整合國考掉落，並修改交卷 API 回傳值與結果頁。
- 口說、寫作完成點的掉落整合與最低完成條件。
- 生活線 LIFF（單一 LIFF app、內部頁面切換）：節氣地圖、答題頁、解說卡、合成結果頁、食材圖鑑。
- 共用圖鑑頁（依 `track` 篩選）。
- 小銅人元件：依 state 顯示圖片，先用佔位圖。
- `entitlements` 模組與開發開關（7.4）。
- Rich menu 建立腳本與 `primary_track` 自動判斷。
- 分享卡 Flex 模板與 LIFF 分享頁。
- 既有聊天室系統提示補上第 9 節第 4 條。

### 等 Momo 提供（先用佔位內容）

| 項目 | 佔位做法 | 替換方式 |
|---|---|---|
| 小銅人 4 張表情圖 | 程式產生的簡單佔位圖 | 放進 `assets/mascot/{state}.webp` |
| 藥材、食材插圖 | 名稱文字卡 | 放進 `assets/herbs/{herb_id}.webp` |
| Rich menu 兩張正式設計圖 | 程式產生的佔位圖 | 替換圖片後重跑建立腳本 |
| 題目內容 | 範例題 + `TODO` 標記 | 修改 seed JSON |
| 茶飲／藥膳文字、君臣佐使說明 | 欄位留空或範例 | 修改 seed JSON |
| 免費額度 N、Pro 定價 | 設定檔預設值 | 修改設定檔 |
| LIFF 視覺樣式（可選） | 簡潔預設樣式 | 提供截圖後調整 |

> 小銅人與主要藥材插圖會直接出現在 demo 影片中，建議優先提供。

### 需要 Momo 在 LINE Developers 後台手動設定（Claude Code 無法代做）

- 生活線 LIFF app **已建立**：LIFF ID `2011870208-kD6N9oKn`（LIFF URL `https://liff.line.me/2011870208-kD6N9oKn`），掛在現有 Channel（`LIFF_CHANNEL_ID=2011870208`）下。**Flask 路由必須是 `/liff/life`**（後台 Endpoint URL 為「Railway 網域 + `/liff/life`」），與現有 `/liff/quiz`、`/liff/speaking`、`/liff/writing` 命名一致。
- 確認該 LIFF 已開啟 shareTargetPicker，scope 含 `profile`、`openid`。
- 確認 Channel 權限與 rich menu 相關設定。

完成後請回報：哪些頁面可以 demo、每個佔位項目的檔案位置、以及上述需手動設定的項目清單。

---

## 12. Demo 腳本（驗收目標，約 1–2 分鐘，需在真機 LINE App 錄製）

1. 開啟官方帳號，看到**生活版 rich menu**。
2. 點「節氣闖關」進入地圖，看到四季 × 節氣與反灰的未開放關卡。
3. 進入一關，完成 3 題（含一題解說卡，小銅人表情變化），過關後**掉落食材卡**（翻牌效果）。
4. 以 demo 帳號預先備好材料，合成 Boss 關的**薑棗茶**，出現合成畫面，點**分享**，shareTargetPicker 選取好友送出。
5. 回到 rich menu，點分頁切換到**學生版**。
6. 進入國考刷題，完成並交卷一份 ≥ 10 題、得分 ≥ 60% 的考卷。
7. 結果頁顯示成績與錯題本，並**翻出 3 張藥材卡**。
8. 開啟藥材圖鑑，展示已收集進度；合成**芍藥甘草湯**並說明君臣佐使。
9. （選）切換為 Pro 帳號，展示 24 關全開與完整食譜。

---

## 13. 待確認事項

- 現有後端語言 / 框架 / 資料庫與 LIFF 部署位置（請 Claude Code 讀 repo 後自行確認）。
- `ref_code` 在 LIFF → 加好友流程中是否可保留（Phase 2 前實測）。
- shareTargetPicker 的分享是否會計入官方帳號的訊息用量（實測：分享前後比對後台用量）。
- 口說最低輪數、寫作最低字數的最終值（待成本表）。
- 免費 AI 額度 N 與分享獎勵上限（待成本表）。
- 生活線題目與茶飲文字的後續審核。
