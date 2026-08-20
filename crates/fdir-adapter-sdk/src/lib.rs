#![forbid(unsafe_code)]
//! Strict, language-neutral adapter protocol and process-boundary contract.
//!
//! This crate implements the Issue #12 development boundary. It defines exact
//! wire envelopes, version/capability negotiation, artifact identity binding,
//! lane-specific receipts, deterministic replay identity, streaming and
//! resource controls, durable non-success outcomes, and the fail-closed
//! isolation receipt required from a supported production launcher. It does not
//! implement a document adapter and does not create a production qualification.

mod protocol;
mod supervisor;

use fdir_core::CapabilityStatus;

pub use protocol::{
    ArtifactHandle, BudgetDimension, BudgetTracker, CapabilityDeclaration, DependencyDeclaration,
    ExecutionRequest, LaneOutput, NativeSubstrateReceipt, NegotiatedSession, NegotiationRequest,
    NetworkPolicy, OcrInferenceReceipt, ProcessBoundary, ProtocolError, ProtocolLane,
    ProtocolSession, QualificationState, RendererObservationReceipt, ReplayIdentity,
    ResourceBudget, ResourceUsage, SemanticCandidateReceipt, SessionState, StorageCodecReceipt,
    TerminalReceipt, WireEnvelope, WireMessageKind, WorkerFailureSignals, WorkerManifest,
    WorkerOutcome, WorkerProvenance, classify_worker_failure, negotiate,
};
pub use supervisor::{
    LaunchRequest, SandboxPolicy, SandboxReceipt, WorkerRegistration, WorkerRegistry,
};

/// Exact schema identifier used by every protocol envelope.
pub const PROTOCOL_SCHEMA: &str = "fdir/adapter-protocol/1";

/// First stable, versioned adapter-protocol contract.
pub const PROTOCOL_VERSION: &str = "1.0.0";

/// The protocol is implemented but no format/capability tuple is qualified.
pub const CAPABILITY: CapabilityStatus =
    CapabilityStatus::implemented_unqualified("adapter-protocol", 12);

/// External, native, FFI, or non-Rust workers receiving untrusted bytes are isolated.
#[must_use]
pub const fn untrusted_document_boundary() -> ProcessBoundary {
    ProcessBoundary::IsolatedWorker
}

#[cfg(test)]
mod tests {
    use super::{CAPABILITY, PROTOCOL_SCHEMA, PROTOCOL_VERSION, ProcessBoundary};

    #[test]
    fn protocol_boundary_is_available_without_a_production_claim() {
        assert_eq!(PROTOCOL_SCHEMA, "fdir/adapter-protocol/1");
        assert_eq!(PROTOCOL_VERSION, "1.0.0");
        let capability = std::hint::black_box(CAPABILITY);
        assert!(capability.available);
        assert!(!capability.production_ready);
    }

    #[test]
    fn untrusted_document_bytes_require_process_isolation() {
        assert_eq!(
            super::untrusted_document_boundary(),
            ProcessBoundary::IsolatedWorker
        );
    }
}
