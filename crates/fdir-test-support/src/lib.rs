#![forbid(unsafe_code)]
//! Deterministic and isolated test facilities shared by Rust integration tests.

use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use fdir_core::{CommandFailure, FailureClass};

static STORE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// Monotonic deterministic clock with an explicit starting instant.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DeterministicClock {
    seconds: u64,
}

impl DeterministicClock {
    /// Create a clock at a deterministic Unix-second value.
    #[must_use]
    pub const fn new(seconds: u64) -> Self {
        Self { seconds }
    }

    /// Read the current deterministic time.
    #[must_use]
    pub const fn current(self) -> u64 {
        self.seconds
    }

    /// Advance by a caller-controlled duration.
    pub fn advance(&mut self, seconds: u64) {
        self.seconds = self.seconds.saturating_add(seconds);
    }
}

/// Small deterministic pseudo-random generator for fixtures and scheduling tests.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DeterministicRng {
    state: u64,
}

impl DeterministicRng {
    /// Create a generator. A zero seed is mapped to a documented nonzero state.
    #[must_use]
    pub const fn new(seed: u64) -> Self {
        let state = if seed == 0 {
            0x4d59_5df4_d0f3_3173
        } else {
            seed
        };
        Self { state }
    }

    /// Produce the next deterministic sample using xorshift64*.
    pub fn sample_u64(&mut self) -> u64 {
        let mut value = self.state;
        value ^= value >> 12;
        value ^= value << 25;
        value ^= value >> 27;
        self.state = value;
        value.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }
}

/// Isolated temporary directory removed when the value is dropped.
#[derive(Debug)]
pub struct TempStore {
    path: PathBuf,
}

impl TempStore {
    /// Create an isolated directory under the platform temporary root.
    pub fn new(seed: u64) -> io::Result<Self> {
        let sequence = STORE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let name = format!("fdir-test-{}-{seed}-{sequence}", std::process::id());
        let path = std::env::temp_dir().join(name);
        fs::create_dir(&path)?;
        Ok(Self { path })
    }

    /// Internal path for tests that need filesystem access.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Stable value suitable for logs and receipts.
    #[must_use]
    pub const fn redacted_path(&self) -> &'static str {
        "<isolated-temp-store>"
    }

    /// Write one fixture below the isolated root without allowing path traversal.
    pub fn write_fixture(&self, relative: &Path, bytes: &[u8]) -> io::Result<PathBuf> {
        if relative.as_os_str().is_empty()
            || relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "fixture path must contain only normal relative components",
            ));
        }
        let destination = self.path.join(relative);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&destination, bytes)?;
        Ok(destination)
    }
}

impl Drop for TempStore {
    fn drop(&mut self) {
        let _ignored = fs::remove_dir_all(&self.path);
    }
}

/// Construct a structured failure for deterministic failure-path tests.
#[must_use]
pub fn synthetic_failure(class: FailureClass) -> CommandFailure {
    CommandFailure::new(class, "FDIR-TEST-SYNTHETIC", "synthetic test failure")
}

#[cfg(test)]
mod tests {
    use std::error::Error;
    use std::path::Path;

    use fdir_core::FailureClass;

    use super::{DeterministicClock, DeterministicRng, TempStore, synthetic_failure};

    #[test]
    fn clock_and_rng_replay_exactly() {
        let mut left = DeterministicRng::new(17);
        let mut right = DeterministicRng::new(17);
        assert_eq!(left.sample_u64(), right.sample_u64());
        assert_eq!(left.sample_u64(), right.sample_u64());

        let mut clock = DeterministicClock::new(100);
        clock.advance(7);
        assert_eq!(clock.current(), 107);
    }

    #[test]
    fn temporary_state_is_isolated_and_redacted() -> Result<(), Box<dyn Error>> {
        let store = TempStore::new(23)?;
        let fixture = store.write_fixture(Path::new("fixtures/input.bin"), b"fixture")?;
        assert!(fixture.starts_with(store.path()));
        assert_eq!(store.redacted_path(), "<isolated-temp-store>");
        assert!(store.write_fixture(Path::new("../escape"), b"bad").is_err());
        Ok(())
    }

    #[test]
    fn synthetic_failures_retain_the_requested_class() {
        let failure = synthetic_failure(FailureClass::Cancelled);
        assert_eq!(failure.class, FailureClass::Cancelled);
    }
}
