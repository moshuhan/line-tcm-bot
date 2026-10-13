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
 *   y 0–200   分頁列：左半「學生」、右半「生活」
 *   y 200–1686 2×2 按鈕，每格 1250×743
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
const W = 2500, H = 1686, TAB_H = 200, CW = W / 2, CH = (H - TAB_H) / 2;

const liffUri = (id, query) => `https://liff.line.me/${id}${query ? '?' + query : ''}`;

function cell(i, action) {
    return { bounds: { x: (i % 2) * CW, y: TAB_H + Math.floor(i / 2) * CH, width: CW, height: CH }, action };
}

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

async function removeOld() {
    const menus = await client.getRichMenuList();
    for (const m of menus) {
        if (m.name === NAME.life || m.name === NAME.student) {
            console.log('刪除舊選單', m.name, m.richMenuId);
            await client.deleteRichMenu(m.richMenuId);
        }
    }
    const aliases = await client.getRichMenuAliasList();
    for (const a of (aliases.aliases || [])) {
        if (a.richMenuAliasId === ALIAS.life || a.richMenuAliasId === ALIAS.student) {
            console.log('刪除舊 alias', a.richMenuAliasId);
            await client.deleteRichMenuAlias(a.richMenuAliasId);
        }
    }
}

async function main() {
    const images = {};
    for (const key of Object.keys(MENUS)) {
        const p = path.join(__dirname, '..', 'assets', 'rich_menu', `${key}.png`);
        if (!fs.existsSync(p)) {
            console.error(`找不到圖片 ${p}，請先執行：python scripts/make_rich_menu_placeholders.py`);
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
    await removeOld();
    const ids = {};
    for (const [key, menu] of Object.entries(MENUS)) {
        ids[key] = await client.createRichMenu(menu);
        await client.setRichMenuImage(ids[key], fs.readFileSync(images[key]), 'image/png');
        await client.createRichMenuAlias(ids[key], ALIAS[key]);
        console.log(`已建立 ${menu.name} -> ${ids[key]}（alias ${ALIAS[key]}）`);
    }
    await client.setDefaultRichMenu(ids.life);
    console.log('預設選單已設為生活版：', ids.life);
}

main().catch(e => {
    console.error('Rich Menu 設定失敗：', e.originalError && e.originalError.response ? e.originalError.response.data : e);
    process.exit(1);
});
