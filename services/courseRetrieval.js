/**
 * services/courseRetrieval.js
 * 以課綱（config/syllabus_full.json / syllabus.json）做「課程資料」的混合檢索：
 * - Keyword matching：命中專有名詞/關鍵字
 * - Vector similarity：topic/keywords 的向量語義相似度
 * - HyDE：先產生虛擬答案再做向量檢索，提高稀疏資料命中率
 *
 * 注意：目前 repo 內能直接讀取的「課程資料」主要是課綱與其 keywords/topic；
 * 真正講義/檔案若是掛在 OpenAI Assistants File Search，仍需由 Assistant 端負責檢索與引用。
 */

const fs = require('fs');
const path = require('path');
const OpenAI = require('openai');

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const SYLLABUS_FULL_PATH = path.join(__dirname, '..', 'config', 'syllabus_full.json');
const SYLLABUS_PATH = path.join(__dirname, '..', 'config', 'syllabus.json');

let cache = {
  loaded: false,
  entries: [],
  embeddingsReady: false,
};

function safeReadJson(filePath) {
  try {
    if (!fs.existsSync(filePath)) return null;
    const raw = fs.readFileSync(filePath, 'utf8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function normalizeText(s) {
  return String(s || '').toLowerCase().trim();
}

function tokenizeForKeyword(text) {
  const t = normalizeText(text);
  // 極簡 token：保留中英文、數字
  return t
    .replace(/[^\p{Script=Han}a-z0-9]+/gu, ' ')
    .split(/\s+/)
    .filter(Boolean);
}

function keywordScore(query, entry) {
  const q = normalizeText(query);
  const tokens = new Set(tokenizeForKeyword(q));

  let score = 0;
  const needles = []
    .concat(entry.keywords || [])
    .concat(entry.topic ? [entry.topic] : [])
    .filter(Boolean)
    .map((x) => String(x));

  for (const needle of needles) {
    const n = normalizeText(needle);
    if (!n) continue;
    // 直接包含：強加權（專有名詞多半可直接命中）
    if (q.includes(n)) score += 6;
    // token 命中：弱加權
    for (const tok of tokenizeForKeyword(n)) {
      if (tokens.has(tok)) score += 1;
    }
  }
  return score;
}

function cosineSimilarity(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return 0;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    const x = a[i];
    const y = b[i];
    dot += x * y;
    na += x * x;
    nb += y * y;
  }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  return denom ? dot / denom : 0;
}

function buildEntries() {
  const full = safeReadJson(SYLLABUS_FULL_PATH);
  const basic = safeReadJson(SYLLABUS_PATH);

  const lectures = Array.isArray(full?.lectures) ? full.lectures : [];

  // syllabus.json 的 keywords（每週）也併入，當作額外 keyword 線索
  const basicLectures = Array.isArray(basic?.lectures) ? basic.lectures : [];

  const extraKeywordsByTitle = new Map();
  for (const lec of basicLectures) {
    const title = String(lec?.title || '').trim();
    const kws = Array.isArray(lec?.keywords) ? lec.keywords : [];
    if (title) extraKeywordsByTitle.set(title, kws.map(String));
  }

  const entries = lectures.map((lec, idx) => {
    const topic = String(lec?.topic || lec?.title || '').trim();
    const date = String(lec?.date || '').trim();
    const hasHandout = Boolean(lec?.has_handout || lec?.hasHandout);
    const kws = []
      .concat(topic ? [topic] : [])
      .concat(extraKeywordsByTitle.get(topic) || [])
      .filter(Boolean);

    const textForEmbedding = [topic, ...kws].filter(Boolean).join(' / ');

    return {
      id: `${date || 'unknown'}:${idx}`,
      date,
      topic,
      hasHandout,
      keywords: Array.from(new Set(kws.map(String))),
      embeddingText: textForEmbedding,
      embedding: null,
    };
  });

  return entries;
}

async function ensureLoaded() {
  if (cache.loaded) return;
  cache.entries = buildEntries();
  cache.loaded = true;
}

async function ensureEmbeddings() {
  await ensureLoaded();
  if (cache.embeddingsReady) return;

  const model = process.env.OPENAI_EMBEDDING_MODEL || 'text-embedding-3-small';
  // 逐筆做 embedding：課綱筆數很少，啟動時一次性成本可接受
  for (const entry of cache.entries) {
    try {
      const resp = await openai.embeddings.create({
        model,
        input: entry.embeddingText || entry.topic || '課程主題',
      });
      entry.embedding = resp.data?.[0]?.embedding || null;
    } catch {
      entry.embedding = null;
    }
  }

  cache.embeddingsReady = true;
}

async function generateHyde(query) {
  const q = String(query || '').trim();
  if (!q) return '';
  try {
    const resp = await openai.chat.completions.create({
      model: 'gpt-4o-mini',
      temperature: 0.2,
      max_tokens: 180,
      messages: [
        {
          role: 'system',
          content:
            '你是中醫課程助教。請針對學生問題先寫出一段「可能的理想答案摘要」（1-3 句），用來協助檢索課程資料；不要加上免責、不必引用來源。',
        },
        { role: 'user', content: q },
      ],
    });
    return String(resp.choices?.[0]?.message?.content || '').trim();
  } catch {
    return '';
  }
}

async function embedText(text) {
  const t = String(text || '').trim();
  if (!t) return null;
  const model = process.env.OPENAI_EMBEDDING_MODEL || 'text-embedding-3-small';
  try {
    const resp = await openai.embeddings.create({ model, input: t });
    return resp.data?.[0]?.embedding || null;
  } catch {
    return null;
  }
}

/**
 * 混合檢索：回傳最相關課程條目與分數。
 */
async function retrieveCourseContext(query, { useHyde = true } = {}) {
  await ensureEmbeddings();

  const q = String(query || '').trim();
  if (!q) return { best: null, candidates: [] };

  const hyde = useHyde ? await generateHyde(q) : '';
  const qEmbedding = await embedText(hyde ? `${q}\n\n${hyde}` : q);

  const scored = cache.entries.map((entry) => {
    const k = keywordScore(q, entry);
    const v = qEmbedding && entry.embedding ? cosineSimilarity(qEmbedding, entry.embedding) : 0;
    // 混合分數：keyword 為主，vector 輔助（向量最高約 1.0）
    const score = k + v * 8;
    return { entry, score, keyword: k, vector: v };
  });

  scored.sort((a, b) => b.score - a.score);
  const top = scored.slice(0, 5);
  return {
    best: top[0] || null,
    candidates: top,
    hyde,
  };
}

module.exports = {
  retrieveCourseContext,
};

