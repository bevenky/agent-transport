//! Synchronization helpers — poisoned-lock recovery.
//!
//! Replaces `.lock().unwrap()` everywhere. If a task panics while holding a
//! `Mutex<T>`, subsequent `lock()` calls return `PoisonError`. Unwrapping that
//! error cascades the panic across unrelated tasks — in the worst case, a
//! single opus-encoder panic takes down recording for every in-flight call on
//! the endpoint.
//!
//! The `LockExt` trait recovers the inner guard via `PoisonError::into_inner()`.
//! The recovered state may be inconsistent (partially updated before the
//! panic), but the alternative is cascading crashes — recovery is strictly
//! better for our voice-agent use cases where a stuck mutex kills live calls.
//!
//! Usage: replace `.lock().unwrap()` with `.lock_or_recover()`.

use std::sync::{Mutex, MutexGuard, RwLock, RwLockReadGuard, RwLockWriteGuard};
use tracing::warn;

/// Extension trait: lock a `Mutex` and recover from poisoning.
pub(crate) trait LockExt<T> {
    fn lock_or_recover(&self) -> MutexGuard<'_, T>;
}

impl<T> LockExt<T> for Mutex<T> {
    #[inline]
    fn lock_or_recover(&self) -> MutexGuard<'_, T> {
        match self.lock() {
            Ok(g) => g,
            Err(e) => {
                warn!("mutex poisoned, recovering: {}", e);
                e.into_inner()
            }
        }
    }
}

/// Extension trait: read/write-lock an `RwLock` and recover from poisoning.
#[allow(dead_code)]
pub(crate) trait RwLockExt<T> {
    fn read_or_recover(&self) -> RwLockReadGuard<'_, T>;
    fn write_or_recover(&self) -> RwLockWriteGuard<'_, T>;
}

impl<T> RwLockExt<T> for RwLock<T> {
    #[inline]
    fn read_or_recover(&self) -> RwLockReadGuard<'_, T> {
        match self.read() {
            Ok(g) => g,
            Err(e) => {
                warn!("rwlock read poisoned, recovering: {}", e);
                e.into_inner()
            }
        }
    }

    #[inline]
    fn write_or_recover(&self) -> RwLockWriteGuard<'_, T> {
        match self.write() {
            Ok(g) => g,
            Err(e) => {
                warn!("rwlock write poisoned, recovering: {}", e);
                e.into_inner()
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::thread;

    #[test]
    fn mutex_recovers_from_poisoned() {
        let m = Arc::new(Mutex::new(42u32));
        let m2 = m.clone();
        let _ = thread::spawn(move || {
            let _g = m2.lock().unwrap();
            panic!("intentional test panic");
        })
        .join();
        assert!(m.lock().is_err(), "mutex should be poisoned");
        // Extension trait still recovers.
        let g = m.lock_or_recover();
        assert_eq!(*g, 42);
    }

    #[test]
    fn mutex_lock_or_recover_on_healthy_mutex() {
        let m = Mutex::new("ok");
        let g = m.lock_or_recover();
        assert_eq!(*g, "ok");
    }

    #[test]
    fn rwlock_recovers_from_poisoned() {
        let rw = Arc::new(RwLock::new(vec![1, 2, 3]));
        let rw2 = rw.clone();
        let _ = thread::spawn(move || {
            let _g = rw2.write().unwrap();
            panic!("intentional test panic");
        })
        .join();
        assert!(rw.read().is_err(), "rwlock should be poisoned");
        let g = rw.read_or_recover();
        assert_eq!(*g, vec![1, 2, 3]);
        drop(g);
        let mut g = rw.write_or_recover();
        g.push(4);
        assert_eq!(*g, vec![1, 2, 3, 4]);
    }
}
