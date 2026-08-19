#![forbid(unsafe_code)]
//! Language-neutral adapter protocol metadata and process-boundary policy.

use fdir_core::CapabilityStatus;

/// Development protocol version exposed by the foundation CLI.
pub const PROTOCOL_VERSION: &str = "0.1.0-dev.1";

/// Adapter execution boundaries recognized by the product architecture.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProcessBoundary {
    /// Safe Rust code that does not parse untrusted document bytes.
    TrustedRust,
    /// A separately constrained worker process.
    IsolatedWorker,
}

/// Full adapter protocol execution is unavailable until Issue #12.
pub const CAPABILITY: CapabilityStatus = CapabilityStatus::unavailable("adapter-protocol", 12);

/// External, native, FFI, or non-Rust document workers are isolated by default.
#[must_use]
pub const fn untrusted_document_boundary() -> ProcessBoundary {
    ProcessBoundary::IsolatedWorker
}

#[cfg(test)]
mod tests {
    use super::{ProcessBoundary, untrusted_document_boundary};

    #[test]
    fn untrusted_document_bytes_require_process_isolation() {
        assert_eq!(
            untrusted_document_boundary(),
            ProcessBoundary::IsolatedWorker
        );
    }
}
