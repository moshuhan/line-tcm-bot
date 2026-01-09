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

async function setupRichMenu() {
    try {
        console.log('Creating Rich Menu...');

        // 1. Create the Rich Menu object
        const richMenu = {
            size: {
                width: 2500,
                height: 843
            },
            selected: false,
            name: 'TCMAcademyMenu',
            chatBarText: '開啟選單',
            areas: [
                {
                    bounds: { x: 0, y: 0, width: 833, height: 843 },
                    action: { type: 'postback', data: 'mode=tcm', displayText: '切換至中醫問答' }
                },
                {
                    bounds: { x: 833, y: 0, width: 833, height: 843 },
                    action: { type: 'postback', data: 'mode=speaking', displayText: '切換至口說練習' }
                },
                {
                    bounds: { x: 1666, y: 0, width: 834, height: 843 },
                    action: { type: 'postback', data: 'mode=writing', displayText: '切換至寫作修改' }
                }
            ]
        };

        const richMenuId = await client.createRichMenu(richMenu);
        console.log('Rich Menu created:', richMenuId);

        // 2. Upload the image
        const imagePath = path.join(__dirname, '..', 'assets', 'rich_menu_background.png');
        if (fs.existsSync(imagePath)) {
            console.log('Uploading image...', imagePath);
            const buffer = fs.readFileSync(imagePath);
            await client.setRichMenuImage(richMenuId, buffer);
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
