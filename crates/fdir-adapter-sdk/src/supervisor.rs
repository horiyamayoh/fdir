#![forbid(unsafe_code)]

use std::collections::BTreeMap;

use fdir_core::Digest;

use crate::{NetworkPolicy, ProcessBoundary, ProtocolError, ResourceBudget, WorkerManifest};

/// Fail-closed production launcher policy. Every permission defaults to denied.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SandboxPolicy {
    pub network_policy: NetworkPolicy,
    pub opaque_handles_only: bool,
    pub isolated_temporary_storage: bool,
    pub clear_environment: bool,
    pub clear_credentials: bool,
    pub deny_child_processes: bool,
    pub read_only_input: bool,
    pub enforce_resource_limits: bool,
}

impl SandboxPolicy {
    /// Strict production policy for an untrusted adapter worker.
    #[must_use]
    pub const fn production_default() -> Self {
        Self {
            network_policy: NetworkPolicy::Deny,
            opaque_handles_only: true,
            isolated_temporary_storage: true,
            clear_environment: true,
            clear_credentials: true,
            deny_child_processes: true,
            read_only_input: true,
            enforce_resource_limits: true,
        }
    }

    /// Reject any relaxation before launch.
    pub fn validate(self) -> Result<Self, ProtocolError> {
        if self != Self::production_default() {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-POLICY-RELAXED",
                "supported production workers require the exact fail-closed sandbox policy",
            ));
        }
        Ok(self)
    }
}

/// Launcher-produced, durable isolation receipt bound to an exact worker launch.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SandboxReceipt {
    pub schema: String,
    pub launcher_id: String,
    pub launcher_version: String,
    pub worker_id: String,
    pub manifest_digest: Digest,
    pub executable_digest: Digest,
    pub policy_digest: Digest,
    pub network_denied: bool,
    pub opaque_handles_only: bool,
    pub isolated_temporary_storage: bool,
    pub environment_cleared: bool,
    pub credentials_cleared: bool,
    pub child_processes_denied: bool,
    pub input_read_only: bool,
    pub resource_limits_enforced: bool,
}

impl SandboxReceipt {
    /// Validate launcher attestation and exact manifest binding.
    pub fn validate(
        &self,
        registration: &WorkerRegistration,
        policy: SandboxPolicy,
    ) -> Result<(), ProtocolError> {
        policy.validate()?;
        if self.schema != "fdir/adapter-sandbox-receipt/1" {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-RECEIPT-SCHEMA",
                "unsupported sandbox receipt schema",
            ));
        }
        if self.launcher_id.is_empty() || self.launcher_version.is_empty() {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-LAUNCHER-IDENTITY",
                "sandbox receipt must identify an exact launcher build",
            ));
        }
        if self.worker_id != registration.manifest.id
            || self.manifest_digest != registration.manifest_digest
            || self.executable_digest != registration.executable_digest
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-IDENTITY-MISMATCH",
                "sandbox receipt differs from the registered worker identity",
            ));
        }
        if !self.network_denied
            || !self.opaque_handles_only
            || !self.isolated_temporary_storage
            || !self.environment_cleared
            || !self.credentials_cleared
            || !self.child_processes_denied
            || !self.input_read_only
            || !self.resource_limits_enforced
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-DENIED",
                "launcher did not attest every required isolation control",
            ));
        }
        Ok(())
    }
}

/// Registry entry resolves an opaque executable identifier; no host path crosses the protocol.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerRegistration {
    pub executable_id: String,
    pub executable_digest: Digest,
    pub manifest_digest: Digest,
    pub manifest: WorkerManifest,
}

impl WorkerRegistration {
    fn validate(&self) -> Result<(), ProtocolError> {
        self.manifest.validate()?;
        if self.executable_id.is_empty()
            || self.executable_id.contains('/')
            || self.executable_id.contains('\\')
            || self.executable_id.contains("..")
            || self.executable_id.chars().any(char::is_whitespace)
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-EXECUTABLE-ID",
                "worker executable id must be opaque and registry-resolved",
            ));
        }
        if self.manifest.receives_untrusted_document_bytes
            && self.manifest.process_boundary != ProcessBoundary::IsolatedWorker
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-PROCESS-BOUNDARY",
                "untrusted document workers must be isolated",
            ));
        }
        Ok(())
    }
}

/// Launch request contains only registry IDs, opaque handles, and bounded arguments.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LaunchRequest {
    pub worker_id: String,
    pub executable_id: String,
    pub artifact_handle: String,
    pub arguments: Vec<String>,
    pub budget: ResourceBudget,
    pub policy: SandboxPolicy,
}

impl LaunchRequest {
    /// Validate that no shell, host path, ambient credential, or unbounded launch enters the boundary.
    pub fn validate(&self, registration: &WorkerRegistration) -> Result<(), ProtocolError> {
        registration.validate()?;
        self.budget.validate()?;
        self.policy.validate()?;
        if self.worker_id != registration.manifest.id
            || self.executable_id != registration.executable_id
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-REGISTRATION-MISMATCH",
                "launch request does not match the registered worker",
            ));
        }
        if !self.artifact_handle.starts_with("artifact:")
            || self.artifact_handle.contains('/')
            || self.artifact_handle.contains('\\')
            || self.artifact_handle.contains("..")
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-ARTIFACT-HANDLE",
                "launcher accepts only opaque artifact handles",
            ));
        }
        if self.arguments.len() > 64
            || self.arguments.iter().any(|argument| {
                argument.len() > 4096
                    || argument.contains('\0')
                    || argument.contains('\n')
                    || argument.contains('\r')
            })
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-ARGUMENT",
                "worker arguments exceed the bounded non-shell argument contract",
            ));
        }
        Ok(())
    }
}

/// Deterministic registry of pre-admitted workers.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct WorkerRegistry {
    workers: BTreeMap<String, WorkerRegistration>,
}

impl WorkerRegistry {
    /// Construct an empty registry that grants no executable capability.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            workers: BTreeMap::new(),
        }
    }

    /// Register one exact worker and reject duplicate identities.
    pub fn register(&mut self, registration: WorkerRegistration) -> Result<(), ProtocolError> {
        registration.validate()?;
        let worker_id = registration.manifest.id.clone();
        if self
            .workers
            .insert(worker_id.clone(), registration)
            .is_some()
        {
            return Err(ProtocolError::new(
                "FDIR-SANDBOX-DUPLICATE-WORKER",
                format!("worker {worker_id} is already registered"),
            ));
        }
        Ok(())
    }

    /// Resolve a worker by stable manifest identifier.
    #[must_use]
    pub fn get(&self, worker_id: &str) -> Option<&WorkerRegistration> {
        self.workers.get(worker_id)
    }

    /// Validate a launch and its launcher receipt before accepting worker output.
    pub fn validate_launch(
        &self,
        request: &LaunchRequest,
        receipt: &SandboxReceipt,
    ) -> Result<(), ProtocolError> {
        let registration = self.get(&request.worker_id).ok_or_else(|| {
            ProtocolError::new(
                "FDIR-SANDBOX-UNKNOWN-WORKER",
                format!("worker {} is not registered", request.worker_id),
            )
        })?;
        request.validate(registration)?;
        receipt.validate(registration, request.policy)
    }
}

#[cfg(test)]
mod tests {
    use fdir_core::Digest;

    use super::{LaunchRequest, SandboxPolicy, SandboxReceipt, WorkerRegistration, WorkerRegistry};
    use crate::{
        CapabilityDeclaration, NetworkPolicy, ProcessBoundary, ProtocolLane, QualificationState,
        ResourceBudget, WorkerManifest,
    };

    const DIGEST_A: &str =
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const DIGEST_B: &str =
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const DIGEST_C: &str =
        "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
    const DIGEST_D: &str =
        "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";

    fn digest(value: &str) -> Result<Digest, Box<dyn std::error::Error>> {
        Digest::new(value.to_owned()).map_err(Into::into)
    }

    fn registration() -> Result<WorkerRegistration, Box<dyn std::error::Error>> {
        Ok(WorkerRegistration {
            executable_id: "mock-python-worker-executable".to_owned(),
            executable_digest: digest(DIGEST_A)?,
            manifest_digest: digest(DIGEST_B)?,
            manifest: WorkerManifest {
                schema: "fdir/adapter-worker-manifest/1".to_owned(),
                id: "mock-python-worker".to_owned(),
                name: "Mock Python worker".to_owned(),
                version: "1.0.0".to_owned(),
                build_digest: digest(DIGEST_C)?,
                implementation_language: "Python".to_owned(),
                protocol_versions: std::collections::BTreeSet::from(["1.0.0".to_owned()]),
                lanes: std::collections::BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
                capabilities: vec![CapabilityDeclaration {
                    id: "test-native-census".to_owned(),
                    profiles: std::collections::BTreeSet::from(["conformance".to_owned()]),
                    lanes: std::collections::BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
                    qualification_state: QualificationState::AdmittedUnqualified,
                }],
                dependencies: Vec::new(),
                normalizations: Vec::new(),
                unavailable_source_distinctions: Vec::new(),
                unsafe_code: false,
                ffi: false,
                native_code: false,
                receives_untrusted_document_bytes: true,
                process_boundary: ProcessBoundary::IsolatedWorker,
                network_policy: NetworkPolicy::Deny,
                qualification_state: QualificationState::AdmittedUnqualified,
                deterministic: true,
                owner_issue: 12,
            },
        })
    }

    fn budget() -> ResourceBudget {
        ResourceBudget {
            max_cpu_millis: 100,
            max_memory_bytes: 4096,
            max_output_bytes: 1024,
            max_objects: 10,
            max_recursion_depth: 8,
            max_decompression_ratio: 20,
            max_wall_clock_millis: 1000,
            max_temporary_storage_bytes: 2048,
            max_chunk_bytes: 512,
            max_in_flight_chunks: 2,
        }
    }

    fn receipt(
        registration: &WorkerRegistration,
    ) -> Result<SandboxReceipt, Box<dyn std::error::Error>> {
        Ok(SandboxReceipt {
            schema: "fdir/adapter-sandbox-receipt/1".to_owned(),
            launcher_id: "linux-sandbox-launcher".to_owned(),
            launcher_version: "1.0.0".to_owned(),
            worker_id: registration.manifest.id.clone(),
            manifest_digest: registration.manifest_digest.clone(),
            executable_digest: registration.executable_digest.clone(),
            policy_digest: digest(DIGEST_D)?,
            network_denied: true,
            opaque_handles_only: true,
            isolated_temporary_storage: true,
            environment_cleared: true,
            credentials_cleared: true,
            child_processes_denied: true,
            input_read_only: true,
            resource_limits_enforced: true,
        })
    }

    #[test]
    fn launch_requires_exact_registry_and_isolation_receipt()
    -> Result<(), Box<dyn std::error::Error>> {
        let registration = registration()?;
        let mut registry = WorkerRegistry::new();
        registry.register(registration.clone())?;
        let request = LaunchRequest {
            worker_id: "mock-python-worker".to_owned(),
            executable_id: "mock-python-worker-executable".to_owned(),
            artifact_handle: "artifact:source-1".to_owned(),
            arguments: vec!["--protocol".to_owned(), "1.0.0".to_owned()],
            budget: budget(),
            policy: SandboxPolicy::production_default(),
        };
        registry.validate_launch(&request, &receipt(&registration)?)?;

        let mut denied = receipt(&registration)?;
        denied.network_denied = false;
        assert_eq!(
            registry
                .validate_launch(&request, &denied)
                .map_err(|error| error.code()),
            Err("FDIR-SANDBOX-DENIED")
        );
        Ok(())
    }

    #[test]
    fn host_paths_and_relaxed_policies_fail_closed() -> Result<(), Box<dyn std::error::Error>> {
        let registration = registration()?;
        let request = LaunchRequest {
            worker_id: "mock-python-worker".to_owned(),
            executable_id: "mock-python-worker-executable".to_owned(),
            artifact_handle: "/tmp/source".to_owned(),
            arguments: Vec::new(),
            budget: budget(),
            policy: SandboxPolicy::production_default(),
        };
        assert_eq!(
            request
                .validate(&registration)
                .map_err(|error| error.code()),
            Err("FDIR-SANDBOX-ARTIFACT-HANDLE")
        );
        let mut relaxed = SandboxPolicy::production_default();
        relaxed.clear_credentials = false;
        assert_eq!(
            relaxed.validate().map_err(|error| error.code()),
            Err("FDIR-SANDBOX-POLICY-RELAXED")
        );
        Ok(())
    }

    #[test]
    fn registry_rejects_duplicates() -> Result<(), Box<dyn std::error::Error>> {
        let registration = registration()?;
        let mut registry = WorkerRegistry::new();
        registry.register(registration.clone())?;
        assert_eq!(
            registry
                .register(registration)
                .map_err(|error| error.code()),
            Err("FDIR-SANDBOX-DUPLICATE-WORKER")
        );
        Ok(())
    }
}
