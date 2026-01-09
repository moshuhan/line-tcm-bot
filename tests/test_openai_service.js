const assert = require('assert');
const proxyquire = require('proxyquire');

// Mock dependencies
const mockState = {
    getThreadId: async () => 'thread_123',
    saveThreadId: async () => { },
};

let lastMessageParams = {};

const mockOpenAI = class {
    constructor() {
        this.beta = {
            threads: {
                create: async () => ({ id: 'thread_new' }),
                messages: {
                    create: async (threadId, params) => {
                        lastMessageParams = params;
                        return {};
                    },
                    list: async () => ({ data: [{ role: 'assistant', content: [{ type: 'text', text: { value: 'Mock Response' } }] }] })
                },
                runs: {
                    create: async () => ({ id: 'run_123' }),
                    retrieve: async () => ({ status: 'completed' })
                }
            }
        };
    }
};

// Intercept require calls
const openaiService = proxyquire('../services/openai', {
    'openai': mockOpenAI,
    './state': mockState
});

async function runTests() {
    console.log('Running tests...');

    // Test 1: Default Mode (TCM)
    await openaiService.handleMessage('user1', 'Hello', 'tcm');
    assert.strictEqual(lastMessageParams.content, 'Hello', 'Default mode should send raw message');
    console.log('Test 1 (Default Mode): PASS');

    // Test 2: Speaking Mode
    await openaiService.handleMessage('user1', 'Hello', 'speaking');
    assert.ok(lastMessageParams.content.startsWith('你現在是口說助教'), 'Speaking mode should prepend instruction');
    assert.ok(lastMessageParams.content.includes('Hello'), 'Speaking mode should include user message');
    console.log('Test 2 (Speaking Mode): PASS');

    // Test 3: Writing Mode (should be same as default/raw for now as we didn't add specific prefix, or did we?)
    // Re-reading implementation: Only 'speaking' has special handling in the prompt injection.
    await openaiService.handleMessage('user1', 'Hello', 'writing');
    assert.strictEqual(lastMessageParams.content, 'Hello', 'Writing mode should send raw message (currently)');
    console.log('Test 3 (Writing Mode): PASS');
}

runTests().catch(err => console.error(err));
