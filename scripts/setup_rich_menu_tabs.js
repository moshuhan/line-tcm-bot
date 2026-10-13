/**
 * Rich Menu：兩套選單（生活版＝預設、學生版），上方「學生｜生活」分頁用 rich menu alias + richmenuswitch 切換。
 *
 * 可重複執行：每次都會先刪掉舊的同名選單與 alias 再重建，之後只要換圖（assets/rich_menu/*.png）重跑即可。
 *   1. python scripts/make_rich_menu_placeholders.py   （沒有正式設計圖時，先產生佔位圖）
 *   2. node scripts/setup_rich_menu_tabs.js            （建立選單、上傳圖片、建立 alias、設定預設為生活版）
 *
 * 注意：會把「預設選單」改成生活版，立刻影響所有使用者；舊的 TCMAcademyLiffMenu 不會被刪除（只是不再是預設）。
 * 不想動到正式帳號時，先用 `--dry-run` 看會做什麼：node scripts/setup_rich_menu_tabs.js --dry-run
 *
 * 版面（2500×1686，需與 make_rich_menu_placeholders.py 一致）：
 *   y 0–170   分頁列：左半「學生」、右半「生活」
 *   y 170–1686 2×2 按鈕：第一列 y 170–887、第二列 y 887–1686，每格寬 1250
 */
const line = require('@line/bot-sdk');
const dotenv = require('dotenv');
const fs = require('fs');
const path = require('path');

dotenv.config();

const DRY_RUN = process.argv.includes('--dry-run');
const client = new line.Client({
    channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN || 'dry-run',
    channelSecret: process.env.LINE_CHANNEL_SECRET || 'dry-run',
});

// LIFF App ID（LINE Developers Console → LIFF 分頁）
const LIFF = {
    life: '2011870208-kD6N9oKn',      // 生活線 → /liff/life
    quiz: '2011870208-ET66KFq7',      // 國考題庫 → /liff/quiz
    speaking: '2011870208-KNn6vb4B',  // 口說教練 → /liff/speaking
    writing: '2011870208-i3BoQwad',   // 寫作教練 → /liff/writing
};

const ALIAS = { life: 'richmenu-alias-life', student: 'richmenu-alias-student' };
const NAME = { life: 'GM_life', student: 'GM_student' };
// 觸控範圍需與 scripts/compose_rich_menu.py 的版面一致：分頁列 y 0–170；第一列 y 170–887、第二列 y 887–1686
const W = 2500, H = 1686, TAB_H = 170, ROW_SPLIT = 887, CW = W / 2;
const ROWS = [{ y: TAB_H, h: ROW_SPLIT - TAB_H }, { y: ROW_SPLIT, h: H - ROW_SPLIT }];

const liffUri = (id, query) => `https://liff.line.me/${id}${query ? '?' + query : ''}`;

function cell(i, action) {
    const row = ROWS[Math.floor(i / 2)];
    return { bounds: { x: (i % 2) * CW, y: row.y, width: CW, height: row.h }, action };
}

// 注意：LINE 規定 richmenuswitch 一定要帶 data，所以每次切換分頁都會對 webhook 送一個 postback（data 以 tab= 開頭），
// 後端 handle_postback 會靜默忽略這種 postback，否則使用者會收到「功能已經更新囉」的提示訊息
function tabs() {
    return [
        { bounds: { x: 0, y: 0, width: CW, height: TAB_H },
          action: { type: 'richmenuswitch', richMenuAliasId: ALIAS.student, data: 'tab=student' } },
        { bounds: { x: CW, y: 0, width: CW, height: TAB_H },
          action: { type: 'richmenuswitch', richMenuAliasId: ALIAS.life, data: 'tab=life' } },
    ];
}

const MENUS = {
    life: {
        size: { width: W, height: H }, selected: true, name: NAME.life, chatBarText: '岐黃選單',
        areas: [
            ...tabs(),
            cell(0, { type: 'uri', label: '節氣闖關', uri: liffUri(LIFF.life, 'page=map') }),
            cell(1, { type: 'uri', label: '食材圖鑑', uri: liffUri(LIFF.life, 'page=codex&track=life') }),
            cell(2, { type: 'uri', label: '今日節氣', uri: liffUri(LIFF.life, 'page=today') }),
            cell(3, { type: 'uri', label: '我的進度', uri: liffUri(LIFF.life, 'page=me') }),
        ],
    },
    student: {
        size: { width: W, height: H }, selected: false, name: NAME.student, chatBarText: '岐黃選單',
        areas: [
            ...tabs(),
            cell(0, { type: 'uri', label: '國考刷題', uri: liffUri(LIFF.quiz) }),
            cell(1, { type: 'uri', label: '英文口說', uri: liffUri(LIFF.speaking) }),
            cell(2, { type: 'uri', label: '英文寫作', uri: liffUri(LIFF.writing) }),
            cell(3, { type: 'uri', label: '藥材圖鑑', uri: liffUri(LIFF.life, 'page=codex&track=student') }),
        ],
    },
};

// 先「建立新的」成功了才刪舊的，避免中途失敗（例如 LINE 驗證不過）把正在使用的預設選單刪掉
async function findOld() {
    const menus = (await client.getRichMenuList()).filter(m => m.name === NAME.life || m.name === NAME.student);
    const aliases = ((await client.getRichMenuAliasList()).aliases || [])
        .filter(a => a.richMenuAliasId === ALIAS.life || a.richMenuAliasId === ALIAS.student);
    return { menus, aliases };
}

async function removeOld(old) {
    for (const a of old.aliases) {
        console.log('刪除舊 alias', a.richMenuAliasId);
        await client.deleteRichMenuAlias(a.richMenuAliasId);
    }
    for (const m of old.menus) {
        console.log('刪除舊選單', m.name, m.richMenuId);
        await client.deleteRichMenu(m.richMenuId);
    }
}

async function main() {
    const images = {};
    for (const key of Object.keys(MENUS)) {
        // 優先用正式設計圖 .jpg（scripts/compose_rich_menu.py 產生，LINE 限制 1MB），沒有再用佔位 .png
        const dir = path.join(__dirname, '..', 'assets', 'rich_menu');
        const jpg = path.join(dir, `${key}.jpg`), png = path.join(dir, `${key}.png`);
        const p = fs.existsSync(jpg) ? jpg : png;
        if (!fs.existsSync(p)) {
            console.error(`找不到圖片 ${jpg}（或 ${png}），請先執行：python scripts/compose_rich_menu.py 或 python scripts/make_rich_menu_placeholders.py`);
            process.exit(1);
        }
        if (fs.statSync(p).size > 1024 * 1024) {
            console.error(`${p} 超過 LINE 的 1MB 限制`);
            process.exit(1);
        }
        images[key] = p;
    }
    if (DRY_RUN) {
        console.log('[dry-run] 不會呼叫 LINE API。預計建立的選單：');
        for (const [k, m] of Object.entries(MENUS)) {
            console.log(`- ${m.name}（image: ${images[k]}）`);
            m.areas.forEach(a => console.log('   ', JSON.stringify(a.bounds), a.action.type, a.action.uri || a.action.richMenuAliasId));
        }
        console.log('預設選單：', NAME.life);
        return;
    }
    const old = await findOld();
    const ids = {};
    for (const [key, menu] of Object.entries(MENUS)) {            // 1. 先建立新選單並上傳圖片（失敗就整個中止，舊的完全沒動）
        ids[key] = await client.createRichMenu(menu);
        await client.setRichMenuImage(ids[key], fs.readFileSync(images[key]), images[key].endsWith('.jpg') ? 'image/jpeg' : 'image/png');
    }
    await client.setDefaultRichMenu(ids.life);                    // 2. 預設先切到新的生活版
    await removeOld(old);                                         // 3. 再刪舊的選單與 alias（alias 名稱不能重複）
    for (const key of Object.keys(MENUS)) {                       // 4. 建立 alias（分頁切換用）
        await client.createRichMenuAlias(ids[key], ALIAS[key]);
        console.log(`已建立 ${MENUS[key].name} -> ${ids[key]}（alias ${ALIAS[key]}）`);
    }
    console.log('預設選單已設為生活版：', ids.life);
}

main().catch(e => {
    console.error('Rich Menu 設定失敗：', e.originalError && e.originalError.response ? e.originalError.response.data : e);
    process.exit(1);
});
