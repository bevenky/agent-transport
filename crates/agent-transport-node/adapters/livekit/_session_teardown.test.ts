/**
 * Unit tests for _session_teardown.ts.
 *
 * Run with: npx tsx --test adapters/livekit/_session_teardown.test.ts
 *
 * Covers:
 *  - `withTimeout` resolves on underlying success, rejection, and timeout
 *  - `runServerCleanup` hangs up active sessions, tolerates failures, and
 *    is bounded by the per-step timeout
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { withTimeout, runServerCleanup, type CleanupContext } from './_session_teardown.js';

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

// ─── runServerCleanup ─────────────────────────────────────────────────

interface Recorder {
  hangups: string[];
  hangupRaisesFor: Set<string>;
  loadMonitorStops: number;
  inferenceCloses: number;
  httpCloses: number;
  endpointShutdowns: number;
}

function makeCtx(overrides: Partial<CleanupContext> & { recorder: Recorder; sessions: string[] }): CleanupContext {
  const { recorder, sessions, ...rest } = overrides;
  return {
    activeSessionIds: () => sessions,
    hangup: (id) => {
      if (recorder.hangupRaisesFor.has(id)) throw new Error(`hangup failed for ${id}`);
      recorder.hangups.push(id);
    },
    stopLoadMonitor: () => {
      recorder.loadMonitorStops++;
    },
    inferenceExecutor: null,
    closeHttpServer: () => {
      recorder.httpCloses++;
    },
    shutdownEndpoint: () => {
      recorder.endpointShutdowns++;
    },
    ...rest,
  };
}

function makeRecorder(): Recorder {
  return {
    hangups: [],
    hangupRaisesFor: new Set(),
    loadMonitorStops: 0,
    inferenceCloses: 0,
    httpCloses: 0,
    endpointShutdowns: 0,
  };
}

test('runServerCleanup hangs up every active session in iteration order', async () => {
  const recorder = makeRecorder();
  const ctx = makeCtx({ recorder, sessions: ['s-1', 's-2', 's-3'] });
  await runServerCleanup(ctx);
  assert.deepEqual(recorder.hangups, ['s-1', 's-2', 's-3']);
  assert.equal(recorder.loadMonitorStops, 1);
  assert.equal(recorder.httpCloses, 1);
  assert.equal(recorder.endpointShutdowns, 1);
});

test('runServerCleanup continues when one hangup throws', async () => {
  // If one hangup raises, cleanup of the rest must still complete — the
  // whole point of force-exit shutdown is that we get out even when one
  // component is misbehaving.
  const recorder = makeRecorder();
  recorder.hangupRaisesFor = new Set(['s-A']);
  const ctx = makeCtx({ recorder, sessions: ['s-A', 's-B'] });
  await runServerCleanup(ctx);
  assert.deepEqual(recorder.hangups, ['s-B']);
  assert.equal(recorder.endpointShutdowns, 1);
});

test('runServerCleanup continues when stopLoadMonitor / closeHttpServer / shutdownEndpoint throw', async () => {
  const recorder = makeRecorder();
  const ctx: CleanupContext = {
    activeSessionIds: () => ['s-1'],
    hangup: (id) => recorder.hangups.push(id),
    stopLoadMonitor: () => {
      recorder.loadMonitorStops++;
      throw new Error('load monitor boom');
    },
    closeHttpServer: () => {
      recorder.httpCloses++;
      throw new Error('http close boom');
    },
    shutdownEndpoint: () => {
      recorder.endpointShutdowns++;
      throw new Error('endpoint shutdown boom');
    },
  };
  await runServerCleanup(ctx); // must not raise
  assert.deepEqual(recorder.hangups, ['s-1']);
  assert.equal(recorder.loadMonitorStops, 1);
  assert.equal(recorder.httpCloses, 1);
  assert.equal(recorder.endpointShutdowns, 1);
});

test('runServerCleanup tolerates missing optional resources', async () => {
  const recorder = makeRecorder();
  const ctx: CleanupContext = {
    activeSessionIds: () => [],
    hangup: () => {
      throw new Error('unreachable — no sessions');
    },
    stopLoadMonitor: () => {
      recorder.loadMonitorStops++;
    },
    // inferenceExecutor / closeHttpServer / shutdownEndpoint all absent
  };
  await runServerCleanup(ctx);
  assert.equal(recorder.loadMonitorStops, 1);
});

test('runServerCleanup is bounded by 2s per-step timeout when inferenceExecutor hangs', async () => {
  // Without the per-step timeout the executor's never-settling promise
  // would prevent cleanup from completing — and the process would never
  // reach the os._exit/process.exit call in the signal-handler wrapper.
  const recorder = makeRecorder();
  const origWarn = console.warn;
  console.warn = () => {}; // silence the timeout warning
  try {
    const ctx = makeCtx({
      recorder,
      sessions: [],
      inferenceExecutor: { close: () => new Promise(() => {}) },
    });
    const t0 = Date.now();
    await runServerCleanup(ctx);
    const elapsed = Date.now() - t0;
    assert.ok(elapsed >= 2000 && elapsed < 3000, `cleanup ran ${elapsed}ms`);
    // Subsequent steps must still have fired after the timeout.
    assert.equal(recorder.endpointShutdowns, 1);
  } finally {
    console.warn = origWarn;
  }
});
