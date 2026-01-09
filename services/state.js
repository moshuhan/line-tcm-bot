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
const userModes = new Map(); // Store user modes: 'tcm' (default), 'speaking', 'writing'

/**
 * Retrieves the OpenAI Thread ID for a given LINE User ID.
 * @param {string} userId - The LINE User ID.
 * @returns {Promise<string|null>} - The OpenAI Thread ID or null if not found.
 */
async function getThreadId(userId) {
  // TODO: Replace with database lookup
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
  db.set(userId, threadId);
  console.log(`[State] Saved Thread ID ${threadId} for User ${userId}`);
}

/**
 * Retrieves the current mode for a given LINE User ID.
 * @param {string} userId - The LINE User ID.
 * @returns {Promise<string>} - The user's mode, defaults to 'tcm'.
 */
async function getUserMode(userId) {
  // TODO: Replace with database lookup
  return userModes.get(userId) || 'tcm';
}

/**
 * Saves the mode for a given LINE User ID.
 * @param {string} userId - The LINE User ID.
 * @param {string} mode - The mode to save ('tcm', 'speaking', 'writing').
 * @returns {Promise<void>}
 */
async function saveUserMode(userId, mode) {
  // TODO: Replace with database save
  userModes.set(userId, mode);
  console.log(`[State] Saved Mode ${mode} for User ${userId}`);
}

module.exports = {
  getThreadId,
  saveThreadId,
  getUserMode,
  saveUserMode
};
