/**
 * lib/db.js
 * 使用 mongoose 連接 MongoDB，提供單一連線實例。
 */

const fs = require('fs');
const path = require('path');

// 從當前檔案所在資料夾往上尋找最近的 .env
let currentDir = __dirname;
const rootDir = path.parse(currentDir).root;
while (true) {
    const envPath = path.join(currentDir, '.env');
    if (fs.existsSync(envPath)) {
        require('dotenv').config({ path: envPath });
        console.log('[MongoDB] 已成功在路徑讀取 .env:', envPath);
        break;
    }
    if (currentDir === rootDir) {
        console.warn('[MongoDB] 未找到 .env 檔案，請確認環境設定。');
        break;
    }
    currentDir = path.dirname(currentDir);
}

const mongoose = require('mongoose');

let connectionPromise = null;

function connect() {
    if (connectionPromise) {
        return connectionPromise;
    }

    const uri = process.env.MONGODB_URI;
    if (!uri) {
        console.warn('[MongoDB] MONGODB_URI not set; skipping connection');
        connectionPromise = Promise.resolve(null);
        return connectionPromise;
    }

    console.log('[MongoDB] Connecting with URI:', uri);

    connectionPromise = mongoose
        .connect(uri, {
            useNewUrlParser: true,
            useUnifiedTopology: true,
        })
        .then(() => {
            console.log('[MongoDB] Connected');
            return mongoose;
        })
        .catch((err) => {
            console.error('[MongoDB] Connection error with URI', uri, '=>', err.message);
            throw err;
        });

    return connectionPromise;
}

module.exports = {
    mongoose,
    connect,
};

