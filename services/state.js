/**
 * services/state.js
 * 
 * This module handles the persistence of OpenAI Thread IDs associated with LINE User IDs.
 * 
 * IMPORTANT:
 * Since Vercel Serverless Functions are stateless, in-memory storage (like the `db` object below)
 * WILL BE LOST whenever the function instance is recycled (cold start).
 * 
 * To enable persistent conversation history, you MUST replace this in-memory implementation
 * with a real database connection (e.g., MongoDB, Redis, Vercel KV, or Supabase).
 */

// In-memory mock database
const db = new Map();

/**
 * Retrieves the OpenAI Thread ID for a given LINE User ID.
 * @param {string} userId - The LINE User ID.
 * @returns {Promise<string|null>} - The OpenAI Thread ID or null if not found.
 */
async function getThreadId(userId) {
  // TODO: Replace with database lookup
  // Example: return await redis.get(`thread:${userId}`);
  return db.get(userId) || null;
}

/**
 * Saves the OpenAI Thread ID for a given LINE User ID.
 * @param {string} userId - The LINE User ID.
 * @param {string} threadId - The OpenAI Thread ID.
 * @returns {Promise<void>}
 */
async function saveThreadId(userId, threadId) {
  // TODO: Replace with database save
  // Example: await redis.set(`thread:${userId}`, threadId);
  db.set(userId, threadId);
  console.log(`[State] Saved Thread ID ${threadId} for User ${userId}`);
}

module.exports = {
  getThreadId,
  saveThreadId
};
