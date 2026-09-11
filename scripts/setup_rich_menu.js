const line = require('@line/bot-sdk');
const dotenv = require('dotenv');
const fs = require('fs');
const path = require('path');

dotenv.config();

const config = {
    channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN,
    channelSecret: process.env.LINE_CHANNEL_SECRET,
};

const client = new line.Client(config);

// LIFF App ID（LINE Developers Console → LIFF 分頁，2026-09-11 建立）
const LIFF_IDS = {
    quiz: '2011558628-8li3MSJw',      // 國考題庫 → /liff/quiz
    speaking: '2011558628-VDHP3SsC',  // 口說教練 → /liff/speaking
    writing: '2011558628-VKytwC2e',   // 寫作教練 → /liff/writing
};

async function setupRichMenu() {
    try {
        console.log('Creating Rich Menu...');

        // 1. Create the Rich Menu object
        // 圖片實際尺寸 2148x732（assets/rich_menu_background_v2.jpg），3 欄等寬 716px，
        // 已取代原本的 3 個 postback 聊天模式切換鈕，全部改成開啟對應 LIFF 頁面。
        const richMenu = {
            size: {
                width: 2148,
                height: 732
            },
            selected: false,
            name: 'TCMAcademyLiffMenu',
            chatBarText: '開啟選單',
            areas: [
                {
                    bounds: { x: 0, y: 0, width: 716, height: 732 },
                    action: { type: 'uri', uri: `line://app/${LIFF_IDS.quiz}`, label: '國考題庫' }
                },
                {
                    bounds: { x: 716, y: 0, width: 716, height: 732 },
                    action: { type: 'uri', uri: `line://app/${LIFF_IDS.speaking}`, label: '口說教練' }
                },
                {
                    bounds: { x: 1432, y: 0, width: 716, height: 732 },
                    action: { type: 'uri', uri: `line://app/${LIFF_IDS.writing}`, label: '寫作教練' }
                }
            ]
        };

        const richMenuId = await client.createRichMenu(richMenu);
        console.log('Rich Menu created:', richMenuId);

        // 2. Upload the image
        const imagePath = path.join(__dirname, '..', 'assets', 'rich_menu_background_v2.jpg');
        if (fs.existsSync(imagePath)) {
            console.log('Uploading image...', imagePath);
            const buffer = fs.readFileSync(imagePath);
            // SDK 的 setRichMenuImage 沒指定 contentType 時預設送 image/png，
            // 這裡上傳的是 .jpg，一定要明確指定，不然 Content-Type 跟實際檔案格式對不上。
            await client.setRichMenuImage(richMenuId, buffer, 'image/jpeg');
            console.log('Image uploaded.');
        } else {
            console.warn('Image not found at', imagePath, '. Please upload manually or run generate_image first.');
        }

        // 3. Set as default
        await client.setDefaultRichMenu(richMenuId);
        console.log('Rich Menu set as default.');

    } catch (error) {
        console.error('Error setting up Rich Menu:', error.originalError ? error.originalError.response.data : error);
    }
}

setupRichMenu();
