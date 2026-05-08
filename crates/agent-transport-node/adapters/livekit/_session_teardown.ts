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

const ACTIVITY_INPUT_GUARD_INSTALLED = Symbol('agentTransportActivityInputGuardInstalled');

function guardActivityInput(activity: any): void {
  if (!activity || activity[ACTIVITY_INPUT_GUARD_INSTALLED]) return;

  try {
    Object.defineProperty(activity, ACTIVITY_INPUT_GUARD_INSTALLED, { value: true });
  } catch {
    activity[ACTIVITY_INPUT_GUARD_INSTALLED] = true;
  }

  if (typeof activity.onEndOfTurn === 'function') {
    activity.onEndOfTurn = async function onEndOfTurnAfterTransportClose(_info: unknown) {
      try {
        if (typeof this.cancelPreemptiveGeneration === 'function') {
          this.cancelPreemptiveGeneration();
        }
      } catch {}
      return true;
    };
  }

  if (typeof activity.onPreemptiveGeneration === 'function') {
    activity.onPreemptiveGeneration = function onPreemptiveGenerationAfterTransportClose() {};
  }
}

/**
 * Synchronously begin tearing down a LiveKit AgentSession on call termination.
 *
 * Guards end-of-turn and preemptive-generation callbacks, closes the audio
 * input (stops feeding STT), clears pending playout, and calls
 * `shutdown({ drain: false })`. Guarding happens before the next I/O tick, so
 * any STT transcript buffered at Deepgram is dropped before it can trigger
 * LLM/TTS on a dead call.
 *
 * Verified against `@livekit/agents` 1.2.x. `session.shutdown()` is public;
 * `activity.onEndOfTurn` / `activity.onPreemptiveGeneration` are private
 * LiveKit hooks; if upstream renames them, issue #83's race can return.
 *
 * Do not set `activity._schedulingPaused` directly on Node. LiveKit's
 * `activity.drain()` treats that flag as "already drained" and skips
 * `agent.onExit()`.
 */
export function forceShutdownAgentSession(session: any): void {
  if (!session) return;

  try {
    const activities = new Set([session.activity, session.nextActivity].filter(Boolean));
    for (const activity of activities) {
      guardActivityInput(activity);
    }
  } catch (e) {
    console.debug('[force-shutdown] activity input guard failed:', e);
  }

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
