/**
 * lib/redisClient.js
 * Redis 連線單例，使用 ioredis。
 * 從 .env 讀取 REDIS_HOST、REDIS_PORT，具 retryStrategy 與 error 日誌。
 */

require('dotenv').config();

const Redis = require('ioredis');

const REDIS_HOST = process.env.REDIS_HOST || '127.0.0.1';
const REDIS_PORT = parseInt(process.env.REDIS_PORT, 10) || 6379;

let client = null;

/**
 * 取得 Redis 連線單例。確保全域只有一個連線。
 * @returns {Redis|null} ioredis 實例，若未建立則建立並回傳。
 */
function getRedisClient() {
    if (client) {
        return client;
    }

    client = new Redis({
        host: REDIS_HOST,
        port: REDIS_PORT,
        retryStrategy(times) {
            const delay = Math.min(times * 100, 3000);
            console.warn(`[Redis] Reconnecting in ${delay}ms (attempt ${times})`);
            return delay;
        },
        maxRetriesPerRequest: null,
        enableReadyCheck: true,
        lazyConnect: true,
    });

    client.on('error', (err) => {
        console.error('[Redis] Error:', err.message);
    });

    client.on('connect', () => {
        console.log('[Redis] Connected');
    });

    client.on('close', () => {
        console.warn('[Redis] Connection closed');
    });

    return client;
}

/**
 * 關閉 Redis 連線（用於 graceful shutdown）。
 */
function closeRedisClient() {
    if (client) {
        client.disconnect();
        client = null;
    }
}

module.exports = {
    getRedisClient,
    closeRedisClient,
};
