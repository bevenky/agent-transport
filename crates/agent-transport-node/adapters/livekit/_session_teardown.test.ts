/**
 * Unit tests for _session_teardown.ts.
 *
 * Run with: npx tsx --test adapters/livekit/_session_teardown.test.ts
 *
 * Covers:
 *  - `withTimeout` resolves on underlying success, rejection, and timeout
 *  - `forceShutdownAgentSession` tolerates None session, missing audio IO,
 *    throwing shutdown, and calls the right pieces in the right order.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { withTimeout, forceShutdownAgentSession } from './_session_teardown.js';

// ─── withTimeout ──────────────────────────────────────────────────────

test('withTimeout resolves when underlying promise resolves first', async () => {
  const start = Date.now();
  await withTimeout(Promise.resolve('ok'), 1000, 'test');
  assert.ok(Date.now() - start < 100, 'should resolve immediately');
});

test('withTimeout swallows rejections from the underlying promise', async () => {
  // Caller expects void; a rejection must NOT surface as UnhandledRejection.
  await withTimeout(Promise.reject(new Error('kaboom')), 1000, 'test');
  // If we get here without crashing the test, the rejection was swallowed.
});

test('withTimeout resolves when the timeout fires first', async () => {
  const start = Date.now();
  const warnings: unknown[] = [];
  const origWarn = console.warn;
  console.warn = (...args) => warnings.push(args);
  try {
    // Promise that never settles.
    await withTimeout(new Promise(() => {}), 50, 'stuck');
  } finally {
    console.warn = origWarn;
  }
  const elapsed = Date.now() - start;
  assert.ok(elapsed >= 50 && elapsed < 500, `timeout elapsed=${elapsed}`);
  assert.ok(
    warnings.some((w) => Array.isArray(w) && String(w[0]).includes('stuck')),
    'should warn about the timed-out label',
  );
});

// ─── forceShutdownAgentSession ────────────────────────────────────────

function makeSession(opts: {
  withAudioIn?: boolean;
  withAudioOut?: boolean;
  shutdownThrows?: boolean;
} = {}) {
  const calls = {
    audioClose: 0,
    audioClearBuffer: 0,
    shutdown: 0,
    shutdownArgs: undefined as unknown,
    activityGuardedAtShutdown: false,
    nextActivityGuardedAtShutdown: false,
    preemptiveGeneration: 0,
    nextPreemptiveGeneration: 0,
    cancelPreemptiveGeneration: 0,
  };
  const originalOnEndOfTurn = async () => false;
  const originalNextOnEndOfTurn = async () => false;
  const originalOnPreemptiveGeneration = () => {
    calls.preemptiveGeneration++;
  };
  const originalNextOnPreemptiveGeneration = () => {
    calls.nextPreemptiveGeneration++;
  };
  const session: any = {
    _closing: false,
    activity: {
      _schedulingPaused: false,
      onEndOfTurn: originalOnEndOfTurn,
      onPreemptiveGeneration: originalOnPreemptiveGeneration,
      cancelPreemptiveGeneration: () => {
        calls.cancelPreemptiveGeneration++;
      },
    },
    nextActivity: {
      _schedulingPaused: false,
      onEndOfTurn: originalNextOnEndOfTurn,
      onPreemptiveGeneration: originalNextOnPreemptiveGeneration,
    },
    input: { audio: null as any },
    output: { audio: null as any },
  };

  if (opts.withAudioIn ?? true) {
    session.input.audio = {
      close: async () => {
        calls.audioClose++;
      },
    };
  }
  if (opts.withAudioOut ?? true) {
    session.output.audio = {
      clearBuffer: () => {
        calls.audioClearBuffer++;
      },
    };
  }
  session.shutdown = (args: unknown) => {
    calls.shutdown++;
    calls.shutdownArgs = args;
    calls.activityGuardedAtShutdown = session.activity.onEndOfTurn !== originalOnEndOfTurn;
    calls.nextActivityGuardedAtShutdown = session.nextActivity.onEndOfTurn !== originalNextOnEndOfTurn;
    if (opts.shutdownThrows) throw new Error('boom');
  };

  return { session, calls };
}

test('forceShutdownAgentSession tolerates null/undefined session', () => {
  // Both must be silent no-ops — ctx._session may be unset on early hangup.
  forceShutdownAgentSession(null);
  forceShutdownAgentSession(undefined);
});

test('forceShutdownAgentSession closes audio input, clears output, calls shutdown(drain:false)', async () => {
  const { session, calls } = makeSession();

  forceShutdownAgentSession(session);

  assert.equal(calls.audioClearBuffer, 1);
  assert.equal(calls.shutdown, 1);
  assert.deepEqual(calls.shutdownArgs, { drain: false });

  // audio input close is fire-and-forget via Promise.resolve().catch(()=>{}) —
  // let microtasks drain before asserting.
  await new Promise((r) => setImmediate(r));
  assert.equal(calls.audioClose, 1);
});

test('forceShutdownAgentSession guards activity input before shutdown', async () => {
  const { session, calls } = makeSession();

  forceShutdownAgentSession(session);

  assert.equal(calls.activityGuardedAtShutdown, true);
  assert.equal(calls.nextActivityGuardedAtShutdown, true);
  assert.equal(session.activity._schedulingPaused, false);
  assert.equal(session.nextActivity._schedulingPaused, false);

  assert.equal(await session.activity.onEndOfTurn({ newTranscript: 'hello' }), true);
  assert.equal(calls.cancelPreemptiveGeneration, 1);

  session.activity.onPreemptiveGeneration({ newTranscript: 'hello' });
  session.nextActivity.onPreemptiveGeneration({ newTranscript: 'hello' });
  assert.equal(calls.preemptiveGeneration, 0);
  assert.equal(calls.nextPreemptiveGeneration, 0);
});

test('forceShutdownAgentSession tolerates missing audio input', () => {
  const { session, calls } = makeSession({ withAudioIn: false });
  forceShutdownAgentSession(session);
  assert.equal(calls.audioClose, 0);
  assert.equal(calls.shutdown, 1);
});

test('forceShutdownAgentSession tolerates missing audio output', () => {
  const { session, calls } = makeSession({ withAudioOut: false });
  forceShutdownAgentSession(session);
  assert.equal(calls.audioClearBuffer, 0);
  assert.equal(calls.shutdown, 1);
});

test('forceShutdownAgentSession tolerates shutdown throwing', () => {
  const { session, calls } = makeSession({ shutdownThrows: true });
  // Must not raise — this is called from the hot event-loop path.
  forceShutdownAgentSession(session);
  assert.equal(calls.shutdown, 1);
});
