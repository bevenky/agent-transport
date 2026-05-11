/**
 * Race a promise against a timeout. Returns void when the promise settles OR
 * the timeout fires (whichever is first). Used in shutdown paths so a wedged
 * resource (unresponsive inference child, keep-alive HTTP connection) can't
 * hang the process indefinitely.
 *
 * Swallows any rejection from `p` so callers don't need to wrap with
 * `.catch(() => {})` — handy in shutdown paths where the only thing we care
 * about is whether it settled in time.
 */
export function withTimeout(p: Promise<unknown>, ms: number, label: string): Promise<void> {
  return new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      console.warn(`[shutdown] ${label} timed out after ${ms}ms — continuing`);
      resolve();
    }, ms);
    Promise.resolve(p)
      .catch(() => {})
      .finally(() => {
        clearTimeout(timer);
        resolve();
      });
  });
}

/**
 * Synchronously begin tearing down a LiveKit AgentSession on call termination.
 *
 * Closes the audio input (stops feeding STT), clears pending playout, and
 * calls `shutdown({ drain: false })` which synchronously unsubscribes the
 * `UserInputTranscribed` handler in its first microtask — before the next
 * I/O tick, so any STT transcript buffered at Deepgram is dropped before
 * it can trigger LLM/TTS on a dead call.
 *
 * Verified against `@livekit/agents` 1.2.x. `session.shutdown()` is public;
 * `session.input.audio.close()` / `session.output.audio.clearBuffer()` are
 * the documented transport override points.
 */
export function forceShutdownAgentSession(session: any): void {
  if (!session) return;

  try {
    const audioIn = session.input?.audio;
    if (audioIn && typeof audioIn.close === 'function') {
      Promise.resolve(audioIn.close()).catch(() => {});
    }
  } catch (e) {
    console.debug('[force-shutdown] audio input close failed:', e);
  }

  try {
    const audioOut = session.output?.audio;
    if (audioOut && typeof audioOut.clearBuffer === 'function') {
      audioOut.clearBuffer();
    }
  } catch (e) {
    console.debug('[force-shutdown] audio output clearBuffer failed:', e);
  }

  try {
    if (typeof session.shutdown === 'function') {
      session.shutdown({ drain: false });
    }
  } catch (e) {
    console.debug('[force-shutdown] session.shutdown failed:', e);
  }
}
