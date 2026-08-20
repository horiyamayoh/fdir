#![forbid(unsafe_code)]

use std::collections::BTreeSet;
use std::error::Error;
use std::fmt::{self, Display, Formatter};

use fdir_core::{CanonicalValue, Digest, EvidenceLane, ObjectValue, ResultState};

use crate::{PROTOCOL_SCHEMA, PROTOCOL_VERSION};

/// Stable protocol validation or state-machine failure.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProtocolError {
    code: &'static str,
    message: String,
}

impl ProtocolError {
    /// Construct a protocol error with a stable machine-readable code.
    #[must_use]
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    /// Stable machine-readable error code.
    #[must_use]
    pub const fn code(&self) -> &'static str {
        self.code
    }

    /// Human-readable explanation that must not contain document bytes or credentials.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for ProtocolError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl Error for ProtocolError {}

/// Adapter execution boundaries recognized by the frozen architecture policy.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum ProcessBoundary {
    /// Trusted product core that does not receive untrusted document bytes through unsafe code.
    TrustedCore,
    /// Explicitly approved in-process component.
    InProcess,
    /// Separately constrained worker process.
    IsolatedWorker,
    /// External service use is forbidden.
    ExternalServiceForbidden,
}

impl ProcessBoundary {
    /// Stable manifest spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::TrustedCore => "trusted-core",
            Self::InProcess => "in-process",
            Self::IsolatedWorker => "isolated-worker",
            Self::ExternalServiceForbidden => "external-service-forbidden",
        }
    }
}

/// Worker network policy.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum NetworkPolicy {
    /// No network namespace access is granted.
    Deny,
    /// Only launcher-enforced endpoints are granted.
    Allowlisted,
    /// Network is a declared requirement; not supported for untrusted production workers.
    Required,
}

impl NetworkPolicy {
    /// Stable manifest spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Deny => "deny",
            Self::Allowlisted => "allowlisted",
            Self::Required => "required",
        }
    }
}

/// Qualification state carried by a worker or advertised capability.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum QualificationState {
    /// Candidate not admitted to the product runtime.
    Candidate,
    /// Admitted development dependency without adapter qualification.
    AdmittedUnqualified,
    /// Qualified only within an adapter-specific boundary.
    AdapterQualified,
    /// Qualified for an exact released production tuple.
    ProductionQualified,
    /// Explicitly rejected.
    Rejected,
}

impl QualificationState {
    /// Stable manifest spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Candidate => "candidate",
            Self::AdmittedUnqualified => "admitted-unqualified",
            Self::AdapterQualified => "adapter-qualified",
            Self::ProductionQualified => "production-qualified",
            Self::Rejected => "rejected",
        }
    }

    fn parse(value: &str) -> Result<Self, ProtocolError> {
        match value {
            "candidate" => Ok(Self::Candidate),
            "admitted-unqualified" => Ok(Self::AdmittedUnqualified),
            "adapter-qualified" => Ok(Self::AdapterQualified),
            "production-qualified" => Ok(Self::ProductionQualified),
            "rejected" => Ok(Self::Rejected),
            _ => Err(ProtocolError::new(
                "FDIR-PROTOCOL-QUALIFICATION",
                format!("unknown qualification state {value:?}"),
            )),
        }
    }

    #[must_use]
    const fn rank(self) -> u8 {
        match self {
            Self::Rejected => 0,
            Self::Candidate => 1,
            Self::AdmittedUnqualified => 2,
            Self::AdapterQualified => 3,
            Self::ProductionQualified => 4,
        }
    }
}

/// Strict evidence lanes used on the adapter wire.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum ProtocolLane {
    /// Exact native bytes, records, selectors, inventory, and census evidence.
    NativeSubstrateCensus,
    /// Semantic interpretation candidate linked to native evidence.
    SemanticHelper,
    /// Renderer measurement linked to source provenance.
    RendererObservation,
    /// OCR or inference observation with method and confidence.
    OcrInferenceObservation,
    /// Deterministic storage or codec output with no interpretation authority.
    StorageCodec,
}

impl ProtocolLane {
    /// Every lane in stable wire order.
    pub const ALL: [Self; 5] = [
        Self::NativeSubstrateCensus,
        Self::SemanticHelper,
        Self::RendererObservation,
        Self::OcrInferenceObservation,
        Self::StorageCodec,
    ];

    /// Stable wire spelling frozen by ADR 0004.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NativeSubstrateCensus => "native-substrate-census",
            Self::SemanticHelper => "semantic-helper",
            Self::RendererObservation => "renderer-observation",
            Self::OcrInferenceObservation => "ocr-inference-observation",
            Self::StorageCodec => "storage-codec",
        }
    }

    /// Map the wire lane to the neutral kernel lane.
    #[must_use]
    pub const fn core_lane(self) -> EvidenceLane {
        match self {
            Self::NativeSubstrateCensus => EvidenceLane::NativeSubstrate,
            Self::SemanticHelper => EvidenceLane::SemanticCandidate,
            Self::RendererObservation => EvidenceLane::Renderer,
            Self::OcrInferenceObservation => EvidenceLane::OcrInference,
            Self::StorageCodec => EvidenceLane::StorageCodec,
        }
    }

    fn parse(value: &str) -> Result<Self, ProtocolError> {
        match value {
            "native-substrate-census" => Ok(Self::NativeSubstrateCensus),
            "semantic-helper" => Ok(Self::SemanticHelper),
            "renderer-observation" => Ok(Self::RendererObservation),
            "ocr-inference-observation" => Ok(Self::OcrInferenceObservation),
            "storage-codec" => Ok(Self::StorageCodec),
            _ => Err(ProtocolError::new(
                "FDIR-PROTOCOL-LANE",
                format!("unknown evidence lane {value:?}"),
            )),
        }
    }
}

/// Opaque, identity-bound reference to an input artifact.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactHandle {
    /// Opaque coordinator-issued handle; never a host path.
    pub handle: String,
    /// Exact content digest of the artifact bytes.
    pub digest: Digest,
    /// Exact byte length bound to the digest.
    pub byte_length: u64,
    /// Declared media type; interpretation remains adapter-owned.
    pub media_type: String,
}

impl ArtifactHandle {
    /// Construct and validate an opaque artifact handle.
    pub fn new(
        handle: impl Into<String>,
        digest: Digest,
        byte_length: u64,
        media_type: impl Into<String>,
    ) -> Result<Self, ProtocolError> {
        let value = Self {
            handle: handle.into(),
            digest,
            byte_length,
            media_type: media_type.into(),
        };
        value.validate()?;
        Ok(value)
    }

    /// Validate identity and reject path-like or empty handles.
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !is_opaque_handle(&self.handle) {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-ARTIFACT-HANDLE",
                "artifact handle must be opaque and must not contain a path",
            ));
        }
        if self.byte_length == 0 {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-ARTIFACT-LENGTH",
                "artifact byte length must be positive",
            ));
        }
        ensure_non_empty(
            &self.media_type,
            "FDIR-PROTOCOL-ARTIFACT-MEDIA-TYPE",
            "artifact media type",
        )
    }
}

/// Exact dependency facts retained in the worker manifest.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DependencyDeclaration {
    pub id: String,
    pub version: String,
    pub features: BTreeSet<String>,
    pub lanes: BTreeSet<ProtocolLane>,
    pub normalizations: Vec<String>,
    pub unavailable_source_distinctions: Vec<String>,
    pub unsafe_code: bool,
    pub ffi: bool,
    pub native_code: bool,
    pub process_boundary: ProcessBoundary,
}

impl DependencyDeclaration {
    /// Validate exact versioning, lane declaration, and unsafe boundary facts.
    pub fn validate(&self) -> Result<(), ProtocolError> {
        ensure_manifest_id(&self.id, "dependency id")?;
        if !is_exact_version(&self.version) {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-DEPENDENCY-VERSION",
                format!(
                    "dependency {} does not have an exact immutable version",
                    self.id
                ),
            ));
        }
        if self.lanes.is_empty() {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-DEPENDENCY-LANES",
                format!("dependency {} declares no evidence lane", self.id),
            ));
        }
        ensure_non_empty_values(
            &self.features,
            "FDIR-PROTOCOL-DEPENDENCY-FEATURE",
            "dependency feature",
        )?;
        ensure_non_empty_slice(
            &self.normalizations,
            "FDIR-PROTOCOL-DEPENDENCY-NORMALIZATION",
            "normalization declaration",
        )?;
        ensure_non_empty_slice(
            &self.unavailable_source_distinctions,
            "FDIR-PROTOCOL-DEPENDENCY-DISTINCTION",
            "unavailable source distinction",
        )?;
        if (self.unsafe_code || self.ffi || self.native_code)
            && self.process_boundary == ProcessBoundary::InProcess
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-UNSAFE-IN-PROCESS",
                format!("dependency {} requires an isolated worker", self.id),
            ));
        }
        Ok(())
    }
}

/// One capability/profile/lane advertisement from a worker.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapabilityDeclaration {
    pub id: String,
    pub profiles: BTreeSet<String>,
    pub lanes: BTreeSet<ProtocolLane>,
    pub qualification_state: QualificationState,
}

impl CapabilityDeclaration {
    /// Validate a complete, bounded capability advertisement.
    pub fn validate(&self) -> Result<(), ProtocolError> {
        ensure_manifest_id(&self.id, "capability id")?;
        if self.profiles.is_empty() || self.lanes.is_empty() {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-CAPABILITY-SCOPE",
                format!("capability {} must declare profiles and lanes", self.id),
            ));
        }
        ensure_non_empty_values(
            &self.profiles,
            "FDIR-PROTOCOL-CAPABILITY-PROFILE",
            "capability profile",
        )
    }
}

/// Signed or content-addressed manifest facts required before negotiation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerManifest {
    pub schema: String,
    pub id: String,
    pub name: String,
    pub version: String,
    pub build_digest: Digest,
    pub implementation_language: String,
    pub protocol_versions: BTreeSet<String>,
    pub lanes: BTreeSet<ProtocolLane>,
    pub capabilities: Vec<CapabilityDeclaration>,
    pub dependencies: Vec<DependencyDeclaration>,
    pub normalizations: Vec<String>,
    pub unavailable_source_distinctions: Vec<String>,
    pub unsafe_code: bool,
    pub ffi: bool,
    pub native_code: bool,
    pub receives_untrusted_document_bytes: bool,
    pub process_boundary: ProcessBoundary,
    pub network_policy: NetworkPolicy,
    pub qualification_state: QualificationState,
    pub deterministic: bool,
    pub owner_issue: u32,
}

impl WorkerManifest {
    /// Validate exact worker identity, dependency facts, isolation, and advertisements.
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema != "fdir/adapter-worker-manifest/1" {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-MANIFEST-SCHEMA",
                format!("unsupported worker manifest schema {:?}", self.schema),
            ));
        }
        ensure_manifest_id(&self.id, "worker id")?;
        ensure_non_empty(&self.name, "FDIR-PROTOCOL-WORKER-NAME", "worker name")?;
        if !is_exact_version(&self.version) {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-WORKER-VERSION",
                "worker version must be exact and immutable",
            ));
        }
        ensure_non_empty(
            &self.implementation_language,
            "FDIR-PROTOCOL-WORKER-LANGUAGE",
            "implementation language",
        )?;
        if self.owner_issue == 0 {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-WORKER-OWNER",
                "worker manifest must name a positive owning issue",
            ));
        }
        if self.protocol_versions.is_empty() || self.lanes.is_empty() {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-WORKER-SCOPE",
                "worker must declare protocol versions and evidence lanes",
            ));
        }
        ensure_non_empty_values(
            &self.protocol_versions,
            "FDIR-PROTOCOL-WORKER-PROTOCOL-VERSION",
            "protocol version",
        )?;
        ensure_non_empty_slice(
            &self.normalizations,
            "FDIR-PROTOCOL-WORKER-NORMALIZATION",
            "worker normalization declaration",
        )?;
        ensure_non_empty_slice(
            &self.unavailable_source_distinctions,
            "FDIR-PROTOCOL-WORKER-DISTINCTION",
            "worker unavailable source distinction",
        )?;
        if self.capabilities.is_empty() {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-WORKER-CAPABILITY",
                "worker manifest must declare at least one capability",
            ));
        }
        let mut capability_ids = BTreeSet::new();
        for capability in &self.capabilities {
            capability.validate()?;
            if !capability_ids.insert(capability.id.as_str()) {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-DUPLICATE-CAPABILITY",
                    format!("duplicate capability {}", capability.id),
                ));
            }
            if !capability.lanes.is_subset(&self.lanes) {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-CAPABILITY-LANE",
                    format!("capability {} advertises an undeclared lane", capability.id),
                ));
            }
            if capability.qualification_state.rank() > self.qualification_state.rank() {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-CAPABILITY-QUALIFICATION",
                    format!("capability {} exceeds worker qualification", capability.id),
                ));
            }
        }
        let mut dependency_ids = BTreeSet::new();
        for dependency in &self.dependencies {
            dependency.validate()?;
            if !dependency_ids.insert(dependency.id.as_str()) {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-DUPLICATE-DEPENDENCY",
                    format!("duplicate dependency {}", dependency.id),
                ));
            }
            if !dependency.lanes.is_subset(&self.lanes) {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-DEPENDENCY-LANE",
                    format!(
                        "dependency {} advertises an undeclared worker lane",
                        dependency.id
                    ),
                ));
            }
        }
        let non_rust_worker = !self.implementation_language.eq_ignore_ascii_case("rust");
        let unsafe_worker = self.unsafe_code || self.ffi || self.native_code;
        if self.receives_untrusted_document_bytes
            && (non_rust_worker || unsafe_worker)
            && self.process_boundary != ProcessBoundary::IsolatedWorker
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-WORKER-ISOLATION",
                "non-Rust, unsafe, FFI, or native workers receiving untrusted bytes must be isolated",
            ));
        }
        if self.receives_untrusted_document_bytes && self.network_policy == NetworkPolicy::Required
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-WORKER-NETWORK",
                "untrusted production workers cannot require ambient network access",
            ));
        }
        if self.qualification_state == QualificationState::ProductionQualified
            && (!self.deterministic || self.network_policy != NetworkPolicy::Deny)
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-WORKER-PRODUCTION",
                "production-qualified workers must be deterministic and network-denied",
            ));
        }
        Ok(())
    }

    fn capability(&self, id: &str) -> Option<&CapabilityDeclaration> {
        self.capabilities
            .iter()
            .find(|capability| capability.id == id)
    }
}

/// Coordinator request for an exact capability/profile/lane session.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NegotiationRequest {
    pub supported_versions: BTreeSet<String>,
    pub capability: String,
    pub profile: String,
    pub required_lanes: BTreeSet<ProtocolLane>,
    pub artifact: ArtifactHandle,
    pub require_production_qualification: bool,
}

/// Exact negotiated session; no capability can appear outside the manifest.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NegotiatedSession {
    pub protocol_version: String,
    pub worker_id: String,
    pub worker_version: String,
    pub manifest_digest: Digest,
    pub capability: String,
    pub profile: String,
    pub lanes: BTreeSet<ProtocolLane>,
    pub artifact: ArtifactHandle,
    pub production_ready: bool,
}

impl NegotiatedSession {
    /// Stable length-prefixed session identity material.
    #[must_use]
    pub fn session_key(&self) -> String {
        length_prefixed([
            self.protocol_version.as_str(),
            self.worker_id.as_str(),
            self.worker_version.as_str(),
            self.manifest_digest.as_str(),
            self.capability.as_str(),
            self.profile.as_str(),
            self.artifact.handle.as_str(),
            self.artifact.digest.as_str(),
        ])
    }
}

/// Negotiate an exact protocol version and manifest-bounded capability.
pub fn negotiate(
    manifest: &WorkerManifest,
    manifest_digest: Digest,
    request: NegotiationRequest,
) -> Result<NegotiatedSession, ProtocolError> {
    manifest.validate()?;
    request.artifact.validate()?;
    ensure_non_empty(
        &request.capability,
        "FDIR-PROTOCOL-NEGOTIATION-CAPABILITY",
        "requested capability",
    )?;
    ensure_non_empty(
        &request.profile,
        "FDIR-PROTOCOL-NEGOTIATION-PROFILE",
        "requested profile",
    )?;
    if !request.supported_versions.contains(PROTOCOL_VERSION)
        || !manifest.protocol_versions.contains(PROTOCOL_VERSION)
    {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-VERSION-MISMATCH",
            "no mutually supported adapter protocol version",
        ));
    }
    let capability = manifest.capability(&request.capability).ok_or_else(|| {
        ProtocolError::new(
            "FDIR-PROTOCOL-CAPABILITY-MISMATCH",
            format!(
                "worker does not advertise capability {}",
                request.capability
            ),
        )
    })?;
    if !capability.profiles.contains(&request.profile) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-PROFILE-MISMATCH",
            format!(
                "capability {} does not advertise profile {}",
                request.capability, request.profile
            ),
        ));
    }
    if request.required_lanes.is_empty() || !request.required_lanes.is_subset(&capability.lanes) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-LANE-MISMATCH",
            "requested lanes are empty or exceed the capability manifest",
        ));
    }
    let production_ready = manifest.qualification_state == QualificationState::ProductionQualified
        && capability.qualification_state == QualificationState::ProductionQualified;
    if request.require_production_qualification && !production_ready {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-UNQUALIFIED-CAPABILITY",
            "negotiation cannot promote an unqualified capability to production-ready",
        ));
    }
    Ok(NegotiatedSession {
        protocol_version: PROTOCOL_VERSION.to_owned(),
        worker_id: manifest.id.clone(),
        worker_version: manifest.version.clone(),
        manifest_digest,
        capability: request.capability,
        profile: request.profile,
        lanes: request.required_lanes,
        artifact: request.artifact,
        production_ready,
    })
}

/// Explicit resource ceilings enforced by coordinator and launcher.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ResourceBudget {
    pub max_cpu_millis: u64,
    pub max_memory_bytes: u64,
    pub max_output_bytes: u64,
    pub max_objects: u64,
    pub max_recursion_depth: u32,
    pub max_decompression_ratio: u32,
    pub max_wall_clock_millis: u64,
    pub max_temporary_storage_bytes: u64,
    pub max_chunk_bytes: u64,
    pub max_in_flight_chunks: u32,
}

impl ResourceBudget {
    /// Validate that every required resource dimension is positive.
    pub fn validate(self) -> Result<Self, ProtocolError> {
        let u64_values = [
            self.max_cpu_millis,
            self.max_memory_bytes,
            self.max_output_bytes,
            self.max_objects,
            self.max_wall_clock_millis,
            self.max_temporary_storage_bytes,
            self.max_chunk_bytes,
        ];
        if u64_values.contains(&0)
            || self.max_recursion_depth == 0
            || self.max_decompression_ratio == 0
            || self.max_in_flight_chunks == 0
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-BUDGET-NONPOSITIVE",
                "every adapter resource budget dimension must be positive",
            ));
        }
        if self.max_chunk_bytes > self.max_output_bytes {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-BUDGET-CHUNK",
                "maximum chunk size cannot exceed the total output budget",
            ));
        }
        Ok(self)
    }
}

/// Cumulative resource usage reported by a worker and checked by the coordinator.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ResourceUsage {
    pub cpu_millis: u64,
    pub peak_memory_bytes: u64,
    pub output_bytes: u64,
    pub object_count: u64,
    pub recursion_depth: u32,
    pub compressed_input_bytes: u64,
    pub decompressed_bytes: u64,
    pub wall_clock_millis: u64,
    pub temporary_storage_bytes: u64,
    pub emitted_chunks: u64,
}

/// Resource dimension that caused fail-closed termination.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BudgetDimension {
    Cpu,
    Memory,
    Output,
    ObjectCount,
    Recursion,
    Decompression,
    WallClock,
    TemporaryStorage,
    Chunk,
    Backpressure,
}

impl BudgetDimension {
    /// Stable diagnostic spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Cpu => "cpu",
            Self::Memory => "memory",
            Self::Output => "output",
            Self::ObjectCount => "object-count",
            Self::Recursion => "recursion",
            Self::Decompression => "decompression",
            Self::WallClock => "wall-clock",
            Self::TemporaryStorage => "temporary-storage",
            Self::Chunk => "chunk",
            Self::Backpressure => "backpressure",
        }
    }
}

/// Deterministic cumulative budget and streaming-window validator.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BudgetTracker {
    budget: ResourceBudget,
    usage: ResourceUsage,
    next_sequence: u64,
    unacknowledged_chunks: BTreeSet<u64>,
    final_sequence: Option<u64>,
}

impl BudgetTracker {
    /// Start a tracker after validating all resource ceilings.
    pub fn new(budget: ResourceBudget) -> Result<Self, ProtocolError> {
        Ok(Self {
            budget: budget.validate()?,
            usage: ResourceUsage::default(),
            next_sequence: 0,
            unacknowledged_chunks: BTreeSet::new(),
            final_sequence: None,
        })
    }

    /// Current cumulative usage.
    #[must_use]
    pub const fn usage(&self) -> ResourceUsage {
        self.usage
    }

    /// Validate a cumulative worker usage report and reject rollback or excess.
    pub fn observe(&mut self, usage: ResourceUsage) -> Result<(), ProtocolError> {
        if usage.cpu_millis < self.usage.cpu_millis
            || usage.peak_memory_bytes < self.usage.peak_memory_bytes
            || usage.output_bytes < self.usage.output_bytes
            || usage.object_count < self.usage.object_count
            || usage.recursion_depth < self.usage.recursion_depth
            || usage.compressed_input_bytes < self.usage.compressed_input_bytes
            || usage.decompressed_bytes < self.usage.decompressed_bytes
            || usage.wall_clock_millis < self.usage.wall_clock_millis
            || usage.temporary_storage_bytes < self.usage.temporary_storage_bytes
            || usage.emitted_chunks < self.usage.emitted_chunks
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-USAGE-ROLLBACK",
                "cumulative worker resource usage cannot decrease",
            ));
        }
        validate_usage(self.budget, usage)?;
        self.usage = usage;
        Ok(())
    }

    /// Admit one ordered output chunk while enforcing chunk and backpressure limits.
    pub fn admit_chunk(
        &mut self,
        sequence: u64,
        byte_length: u64,
        final_chunk: bool,
    ) -> Result<(), ProtocolError> {
        if self.final_sequence.is_some() {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-CHUNK-AFTER-FINAL",
                "no chunk may follow the final chunk",
            ));
        }
        if sequence != self.next_sequence {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-CHUNK-SEQUENCE",
                format!("expected chunk {}, received {sequence}", self.next_sequence),
            ));
        }
        if byte_length == 0 || byte_length > self.budget.max_chunk_bytes {
            return Err(resource_error(BudgetDimension::Chunk));
        }
        if self.unacknowledged_chunks.len()
            >= usize::try_from(self.budget.max_in_flight_chunks).unwrap_or(usize::MAX)
        {
            return Err(resource_error(BudgetDimension::Backpressure));
        }
        let output_bytes = self
            .usage
            .output_bytes
            .checked_add(byte_length)
            .ok_or_else(|| resource_error(BudgetDimension::Output))?;
        let emitted_chunks = self
            .usage
            .emitted_chunks
            .checked_add(1)
            .ok_or_else(|| resource_error(BudgetDimension::Output))?;
        let mut next_usage = self.usage;
        next_usage.output_bytes = output_bytes;
        next_usage.emitted_chunks = emitted_chunks;
        validate_usage(self.budget, next_usage)?;
        self.usage = next_usage;
        self.unacknowledged_chunks.insert(sequence);
        self.next_sequence = self.next_sequence.checked_add(1).ok_or_else(|| {
            ProtocolError::new("FDIR-PROTOCOL-CHUNK-SEQUENCE", "chunk sequence overflow")
        })?;
        if final_chunk {
            self.final_sequence = Some(sequence);
        }
        Ok(())
    }

    /// Acknowledge one in-flight chunk; unknown or repeated acknowledgements fail closed.
    pub fn acknowledge(&mut self, sequence: u64) -> Result<(), ProtocolError> {
        if !self.unacknowledged_chunks.remove(&sequence) {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-CHUNK-ACK",
                format!("chunk {sequence} is not awaiting acknowledgement"),
            ));
        }
        Ok(())
    }

    /// Whether a final chunk was seen and every chunk was acknowledged.
    #[must_use]
    pub fn stream_complete(&self) -> bool {
        self.final_sequence.is_some() && self.unacknowledged_chunks.is_empty()
    }
}

/// Exact replay identity; operational timestamps are deliberately excluded.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReplayIdentity {
    pub request_id: String,
    pub artifact_digest: Digest,
    pub manifest_digest: Digest,
    pub worker_version: String,
    pub configuration_digest: Digest,
    pub context_digest: Option<Digest>,
    pub capability: String,
    pub profile: String,
    pub lanes: BTreeSet<ProtocolLane>,
}

impl ReplayIdentity {
    /// Deterministic length-prefixed key for idempotency and retry comparison.
    #[must_use]
    pub fn key(&self) -> String {
        let lane_text = self
            .lanes
            .iter()
            .map(|lane| lane.as_str())
            .collect::<Vec<_>>()
            .join(",");
        length_prefixed([
            self.request_id.as_str(),
            self.artifact_digest.as_str(),
            self.manifest_digest.as_str(),
            self.worker_version.as_str(),
            self.configuration_digest.as_str(),
            self.context_digest.as_ref().map_or("", Digest::as_str),
            self.capability.as_str(),
            self.profile.as_str(),
            lane_text.as_str(),
        ])
    }
}

/// Fully identity-bound request admitted to a negotiated session.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExecutionRequest {
    pub session_key: String,
    pub replay_identity: ReplayIdentity,
    pub replay_key: String,
    pub artifact: ArtifactHandle,
    pub manifest_digest: Digest,
    pub capability: String,
    pub profile: String,
    pub lanes: BTreeSet<ProtocolLane>,
    pub budget: ResourceBudget,
}

impl ExecutionRequest {
    /// Validate every session, artifact, capability, lane, and replay binding.
    pub fn validate(&self, session: &NegotiatedSession) -> Result<(), ProtocolError> {
        self.artifact.validate()?;
        self.budget.validate()?;
        if self.session_key != session.session_key() {
            return Err(identity_error("session key"));
        }
        if self.artifact != session.artifact {
            return Err(identity_error("artifact handle, digest, or length"));
        }
        if self.manifest_digest != session.manifest_digest {
            return Err(identity_error("worker manifest digest"));
        }
        if self.capability != session.capability || self.profile != session.profile {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-REQUEST-SCOPE",
                "execution capability or profile differs from negotiation",
            ));
        }
        if self.lanes != session.lanes {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-REQUEST-LANES",
                "execution lanes differ from negotiation",
            ));
        }
        if self.replay_key != self.replay_identity.key() {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-REPLAY-KEY",
                "replay key does not match deterministic request identity",
            ));
        }
        if self.replay_identity.artifact_digest != self.artifact.digest
            || self.replay_identity.manifest_digest != self.manifest_digest
            || self.replay_identity.worker_version != session.worker_version
            || self.replay_identity.capability != self.capability
            || self.replay_identity.profile != self.profile
            || self.replay_identity.lanes != self.lanes
        {
            return Err(identity_error("replay identity"));
        }
        Ok(())
    }
}

/// Native substrate or census receipt. It cannot carry a semantic assertion payload.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeSubstrateReceipt {
    pub inventory_item_id: String,
    pub selector: CanonicalValue,
    pub evidence_digest: Digest,
}

/// Semantic candidate linked to native source occurrences.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SemanticCandidateReceipt {
    pub candidate_id: String,
    pub source_occurrence_ids: Vec<String>,
    pub value: CanonicalValue,
}

/// Renderer observation linked to source occurrences and an exact renderer build.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RendererObservationReceipt {
    pub observation_id: String,
    pub source_occurrence_ids: Vec<String>,
    pub renderer_version: String,
    pub value: CanonicalValue,
}

/// OCR or inference observation with an explicit method and bounded confidence.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OcrInferenceReceipt {
    pub observation_id: String,
    pub source_occurrence_ids: Vec<String>,
    pub method: String,
    pub confidence_millionths: u32,
    pub value: CanonicalValue,
}

/// Storage or codec receipt that carries no source-interpretation authority.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StorageCodecReceipt {
    pub object_digest: Digest,
    pub byte_length: u64,
}

/// Lane-discriminated output; no generic payload permits cross-lane substitution.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LaneOutput {
    NativeSubstrate(NativeSubstrateReceipt),
    SemanticCandidate(SemanticCandidateReceipt),
    RendererObservation(RendererObservationReceipt),
    OcrInferenceObservation(OcrInferenceReceipt),
    StorageCodec(StorageCodecReceipt),
}

impl LaneOutput {
    /// Exact lane implied by the Rust variant.
    #[must_use]
    pub const fn lane(&self) -> ProtocolLane {
        match self {
            Self::NativeSubstrate(_) => ProtocolLane::NativeSubstrateCensus,
            Self::SemanticCandidate(_) => ProtocolLane::SemanticHelper,
            Self::RendererObservation(_) => ProtocolLane::RendererObservation,
            Self::OcrInferenceObservation(_) => ProtocolLane::OcrInferenceObservation,
            Self::StorageCodec(_) => ProtocolLane::StorageCodec,
        }
    }

    /// Validate lane-specific source and provenance requirements.
    pub fn validate(&self) -> Result<(), ProtocolError> {
        match self {
            Self::NativeSubstrate(receipt) => {
                ensure_non_empty(
                    &receipt.inventory_item_id,
                    "FDIR-PROTOCOL-NATIVE-ITEM",
                    "native inventory item id",
                )?;
            }
            Self::SemanticCandidate(receipt) => {
                ensure_non_empty(
                    &receipt.candidate_id,
                    "FDIR-PROTOCOL-SEMANTIC-CANDIDATE",
                    "semantic candidate id",
                )?;
                require_source_occurrences(&receipt.source_occurrence_ids)?;
            }
            Self::RendererObservation(receipt) => {
                ensure_non_empty(
                    &receipt.observation_id,
                    "FDIR-PROTOCOL-RENDERER-OBSERVATION",
                    "renderer observation id",
                )?;
                require_source_occurrences(&receipt.source_occurrence_ids)?;
                if !is_exact_version(&receipt.renderer_version) {
                    return Err(ProtocolError::new(
                        "FDIR-PROTOCOL-RENDERER-VERSION",
                        "renderer version must be exact and immutable",
                    ));
                }
            }
            Self::OcrInferenceObservation(receipt) => {
                ensure_non_empty(
                    &receipt.observation_id,
                    "FDIR-PROTOCOL-OCR-OBSERVATION",
                    "OCR or inference observation id",
                )?;
                require_source_occurrences(&receipt.source_occurrence_ids)?;
                ensure_non_empty(
                    &receipt.method,
                    "FDIR-PROTOCOL-OCR-METHOD",
                    "OCR or inference method",
                )?;
                if receipt.confidence_millionths > 1_000_000 {
                    return Err(ProtocolError::new(
                        "FDIR-PROTOCOL-OCR-CONFIDENCE",
                        "confidence must be between 0 and 1,000,000 millionths",
                    ));
                }
            }
            Self::StorageCodec(receipt) => {
                if receipt.byte_length == 0 {
                    return Err(ProtocolError::new(
                        "FDIR-PROTOCOL-STORAGE-LENGTH",
                        "storage or codec output length must be positive",
                    ));
                }
            }
        }
        Ok(())
    }
}

/// Durable terminal outcome; operational failures remain distinct.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum WorkerOutcome {
    Complete,
    Partial,
    Unsupported,
    Unresolved,
    Cancelled,
    Failed,
    Unreadable,
    ResourceLimited,
    PolicyExcluded,
    TimedOut,
    WorkerCrash,
    SandboxDenied,
    ProtocolMismatch,
    IdentityMismatch,
    MalformedResponse,
    TruncatedOutput,
}

impl WorkerOutcome {
    /// Every outcome in stable presentation order.
    pub const ALL: [Self; 16] = [
        Self::Complete,
        Self::Partial,
        Self::Unsupported,
        Self::Unresolved,
        Self::Cancelled,
        Self::Failed,
        Self::Unreadable,
        Self::ResourceLimited,
        Self::PolicyExcluded,
        Self::TimedOut,
        Self::WorkerCrash,
        Self::SandboxDenied,
        Self::ProtocolMismatch,
        Self::IdentityMismatch,
        Self::MalformedResponse,
        Self::TruncatedOutput,
    ];

    /// Stable wire spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Complete => "complete",
            Self::Partial => "partial",
            Self::Unsupported => "unsupported",
            Self::Unresolved => "unresolved",
            Self::Cancelled => "cancelled",
            Self::Failed => "failed",
            Self::Unreadable => "unreadable",
            Self::ResourceLimited => "resource-limited",
            Self::PolicyExcluded => "policy-excluded",
            Self::TimedOut => "timed-out",
            Self::WorkerCrash => "worker-crash",
            Self::SandboxDenied => "sandbox-denied",
            Self::ProtocolMismatch => "protocol-mismatch",
            Self::IdentityMismatch => "identity-mismatch",
            Self::MalformedResponse => "malformed-response",
            Self::TruncatedOutput => "truncated-output",
        }
    }

    /// Neutral kernel state without erasing the more specific protocol outcome.
    #[must_use]
    pub const fn result_state(self) -> ResultState {
        match self {
            Self::Complete => ResultState::Complete,
            Self::Partial | Self::TruncatedOutput => ResultState::Partial,
            Self::Unsupported => ResultState::Unsupported,
            Self::Unresolved => ResultState::Unresolved,
            Self::Cancelled => ResultState::Cancelled,
            Self::Unreadable => ResultState::Unreadable,
            Self::ResourceLimited | Self::TimedOut => ResultState::ResourceLimited,
            Self::PolicyExcluded | Self::SandboxDenied => ResultState::PolicyExcluded,
            Self::Failed
            | Self::WorkerCrash
            | Self::ProtocolMismatch
            | Self::IdentityMismatch
            | Self::MalformedResponse => ResultState::Failed,
        }
    }

    fn parse(value: &str) -> Result<Self, ProtocolError> {
        Self::ALL
            .into_iter()
            .find(|outcome| outcome.as_str() == value)
            .ok_or_else(|| {
                ProtocolError::new(
                    "FDIR-PROTOCOL-OUTCOME",
                    format!("unknown worker outcome {value:?}"),
                )
            })
    }
}

/// Exact worker, dependency, configuration, and platform provenance for a result.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerProvenance {
    pub worker_id: String,
    pub worker_version: String,
    pub build_digest: Digest,
    pub configuration_digest: Digest,
    pub platform: String,
    pub dependency_ids: Vec<String>,
}

impl WorkerProvenance {
    fn validate(&self) -> Result<(), ProtocolError> {
        ensure_manifest_id(&self.worker_id, "provenance worker id")?;
        if !is_exact_version(&self.worker_version) {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-PROVENANCE-WORKER-VERSION",
                "provenance worker version must be exact",
            ));
        }
        ensure_non_empty(
            &self.platform,
            "FDIR-PROTOCOL-PROVENANCE-PLATFORM",
            "worker platform",
        )?;
        ensure_non_empty_slice(
            &self.dependency_ids,
            "FDIR-PROTOCOL-PROVENANCE-DEPENDENCY",
            "provenance dependency id",
        )
    }
}

/// Identity-bound final receipt persisted even for non-success outcomes.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TerminalReceipt {
    pub request_id: String,
    pub artifact_digest: Digest,
    pub manifest_digest: Digest,
    pub outcome: WorkerOutcome,
    pub output_complete: bool,
    pub retryable: bool,
    pub diagnostic_code: String,
    pub usage: ResourceUsage,
    pub provenance: WorkerProvenance,
}

impl TerminalReceipt {
    /// Validate identity, resource, terminal-state, and provenance invariants.
    pub fn validate(
        &self,
        request: &ExecutionRequest,
        tracker: &BudgetTracker,
    ) -> Result<(), ProtocolError> {
        ensure_non_empty(
            &self.request_id,
            "FDIR-PROTOCOL-TERMINAL-REQUEST",
            "terminal request id",
        )?;
        if self.request_id != request.replay_identity.request_id
            || self.artifact_digest != request.artifact.digest
            || self.manifest_digest != request.manifest_digest
        {
            return Err(identity_error("terminal receipt"));
        }
        ensure_non_empty(
            &self.diagnostic_code,
            "FDIR-PROTOCOL-TERMINAL-DIAGNOSTIC",
            "terminal diagnostic code",
        )?;
        self.provenance.validate()?;
        validate_usage(request.budget, self.usage)?;
        if self.outcome == WorkerOutcome::Complete
            && (!self.output_complete || self.retryable || !tracker.stream_complete())
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-FALSE-COMPLETE",
                "complete outcome requires a complete acknowledged stream and cannot be retryable",
            ));
        }
        if self.outcome == WorkerOutcome::TruncatedOutput && self.output_complete {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-TRUNCATED-COMPLETE",
                "truncated output cannot claim output completeness",
            ));
        }
        Ok(())
    }
}

/// Protocol session state; terminal sessions cannot accept more output.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SessionState {
    Running,
    Cancelling,
    Terminal,
}

/// Stateful validator shared by Rust workers and language-neutral worker harnesses.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProtocolSession {
    session: NegotiatedSession,
    request: ExecutionRequest,
    state: SessionState,
    next_output_sequence: u64,
    tracker: BudgetTracker,
    terminal: Option<TerminalReceipt>,
}

impl ProtocolSession {
    /// Start a session after validating all negotiated and replay identity material.
    pub fn start(
        session: NegotiatedSession,
        request: ExecutionRequest,
    ) -> Result<Self, ProtocolError> {
        request.validate(&session)?;
        let tracker = BudgetTracker::new(request.budget)?;
        Ok(Self {
            session,
            request,
            state: SessionState::Running,
            next_output_sequence: 0,
            tracker,
            terminal: None,
        })
    }

    /// Current session state.
    #[must_use]
    pub const fn state(&self) -> SessionState {
        self.state
    }

    /// Access the deterministic budget tracker.
    #[must_use]
    pub const fn tracker(&self) -> &BudgetTracker {
        &self.tracker
    }

    /// Admit one typed lane output and its ordered chunk metadata.
    pub fn accept_output(
        &mut self,
        sequence: u64,
        byte_length: u64,
        final_chunk: bool,
        output: &LaneOutput,
    ) -> Result<(), ProtocolError> {
        if self.state != SessionState::Running {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-OUTPUT-STATE",
                "output is accepted only while a session is running",
            ));
        }
        if sequence != self.next_output_sequence {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-OUTPUT-SEQUENCE",
                format!(
                    "expected output {}, received {sequence}",
                    self.next_output_sequence
                ),
            ));
        }
        output.validate()?;
        if !self.session.lanes.contains(&output.lane()) {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-OUTPUT-LANE",
                "worker output lane was not negotiated",
            ));
        }
        self.tracker
            .admit_chunk(sequence, byte_length, final_chunk)?;
        self.next_output_sequence = self.next_output_sequence.checked_add(1).ok_or_else(|| {
            ProtocolError::new("FDIR-PROTOCOL-OUTPUT-SEQUENCE", "output sequence overflow")
        })?;
        Ok(())
    }

    /// Acknowledge one admitted chunk to provide deterministic backpressure.
    pub fn acknowledge_output(&mut self, sequence: u64) -> Result<(), ProtocolError> {
        self.tracker.acknowledge(sequence)
    }

    /// Record cancellation before the worker terminal receipt arrives.
    pub fn cancel(&mut self) -> Result<(), ProtocolError> {
        if self.state != SessionState::Running {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-CANCEL-STATE",
                "only a running session can enter cancellation",
            ));
        }
        self.state = SessionState::Cancelling;
        Ok(())
    }

    /// Apply a cumulative resource observation.
    pub fn observe_usage(&mut self, usage: ResourceUsage) -> Result<(), ProtocolError> {
        if self.state == SessionState::Terminal {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-USAGE-AFTER-TERMINAL",
                "resource usage cannot change after terminal state",
            ));
        }
        self.tracker.observe(usage)
    }

    /// Finish with a durable terminal receipt.
    pub fn finish(&mut self, receipt: TerminalReceipt) -> Result<(), ProtocolError> {
        if self.state == SessionState::Terminal {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-DUPLICATE-TERMINAL",
                "a session may have exactly one terminal receipt",
            ));
        }
        if self.state == SessionState::Cancelling && receipt.outcome == WorkerOutcome::Complete {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-CANCELLED-COMPLETE",
                "a cancelled request cannot transition to complete",
            ));
        }
        receipt.validate(&self.request, &self.tracker)?;
        self.state = SessionState::Terminal;
        self.terminal = Some(receipt);
        Ok(())
    }

    /// Borrow the durable terminal receipt after completion.
    #[must_use]
    pub const fn terminal(&self) -> Option<&TerminalReceipt> {
        self.terminal.as_ref()
    }
}

/// Launcher and protocol facts used to classify a non-success worker result.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct WorkerFailureSignals {
    pub timed_out: bool,
    pub cancelled: bool,
    pub sandbox_denied: bool,
    pub protocol_mismatch: bool,
    pub identity_mismatch: bool,
    pub malformed_response: bool,
    pub truncated_output: bool,
    pub exit_success: bool,
}

/// Classify launcher and protocol failures without coercing them to success.
#[must_use]
pub const fn classify_worker_failure(signals: WorkerFailureSignals) -> WorkerOutcome {
    if signals.cancelled {
        WorkerOutcome::Cancelled
    } else if signals.timed_out {
        WorkerOutcome::TimedOut
    } else if signals.sandbox_denied {
        WorkerOutcome::SandboxDenied
    } else if signals.protocol_mismatch {
        WorkerOutcome::ProtocolMismatch
    } else if signals.identity_mismatch {
        WorkerOutcome::IdentityMismatch
    } else if signals.malformed_response {
        WorkerOutcome::MalformedResponse
    } else if signals.truncated_output {
        WorkerOutcome::TruncatedOutput
    } else if signals.exit_success {
        WorkerOutcome::Failed
    } else {
        WorkerOutcome::WorkerCrash
    }
}

/// Strict wire message kinds.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum WireMessageKind {
    ClientHello,
    WorkerHello,
    Execute,
    Cancel,
    Chunk,
    Output,
    Terminal,
}

impl WireMessageKind {
    /// Stable wire spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ClientHello => "client-hello",
            Self::WorkerHello => "worker-hello",
            Self::Execute => "execute",
            Self::Cancel => "cancel",
            Self::Chunk => "chunk",
            Self::Output => "output",
            Self::Terminal => "terminal",
        }
    }

    fn parse(value: &str) -> Result<Self, ProtocolError> {
        match value {
            "client-hello" => Ok(Self::ClientHello),
            "worker-hello" => Ok(Self::WorkerHello),
            "execute" => Ok(Self::Execute),
            "cancel" => Ok(Self::Cancel),
            "chunk" => Ok(Self::Chunk),
            "output" => Ok(Self::Output),
            "terminal" => Ok(Self::Terminal),
            _ => Err(ProtocolError::new(
                "FDIR-PROTOCOL-MESSAGE-KIND",
                format!("unknown protocol message kind {value:?}"),
            )),
        }
    }
}

/// Strictly decoded wire envelope. Unknown fields fail before body use.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WireEnvelope {
    pub kind: WireMessageKind,
    pub session_id: String,
    pub request_id: String,
    pub sequence: u64,
    pub critical: BTreeSet<String>,
    pub body: ObjectValue,
}

impl WireEnvelope {
    /// Parse and validate one exact JSON envelope.
    pub fn decode_json(text: &str) -> Result<Self, ProtocolError> {
        let value = CanonicalValue::parse_json(text).map_err(|error| {
            ProtocolError::new("FDIR-PROTOCOL-MALFORMED-JSON", error.to_string())
        })?;
        let object = value.as_object().ok_or_else(|| {
            ProtocolError::new(
                "FDIR-PROTOCOL-ENVELOPE-TYPE",
                "protocol envelope must be a JSON object",
            )
        })?;
        reject_unknown_fields(
            object,
            &[
                "schema",
                "protocolVersion",
                "kind",
                "sessionId",
                "requestId",
                "sequence",
                "critical",
                "body",
            ],
            "FDIR-PROTOCOL-UNKNOWN-FIELD",
        )?;
        let schema = required_string(object, "schema")?;
        if schema != PROTOCOL_SCHEMA {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-SCHEMA-MISMATCH",
                format!("unsupported protocol schema {schema:?}"),
            ));
        }
        let version = required_string(object, "protocolVersion")?;
        if version != PROTOCOL_VERSION {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-VERSION-MISMATCH",
                format!("unsupported protocol version {version:?}"),
            ));
        }
        let kind = WireMessageKind::parse(required_string(object, "kind")?)?;
        let session_id = required_non_empty_string(object, "sessionId")?.to_owned();
        let request_id = required_non_empty_string(object, "requestId")?.to_owned();
        let sequence = required_u64(object, "sequence")?;
        let critical = required_string_set(object, "critical")?;
        let body = required_object(object, "body")?.clone();
        validate_wire_body(kind, &body)?;
        validate_critical_fields(&critical, object, &body, kind)?;
        Ok(Self {
            kind,
            session_id,
            request_id,
            sequence,
            critical,
            body,
        })
    }

    /// Return the lane of a chunk or output message after strict body validation.
    pub fn lane(&self) -> Result<Option<ProtocolLane>, ProtocolError> {
        if matches!(self.kind, WireMessageKind::Chunk | WireMessageKind::Output) {
            return ProtocolLane::parse(required_string(&self.body, "lane")?).map(Some);
        }
        Ok(None)
    }
}

fn validate_wire_body(kind: WireMessageKind, body: &ObjectValue) -> Result<(), ProtocolError> {
    match kind {
        WireMessageKind::ClientHello => validate_client_hello(body),
        WireMessageKind::WorkerHello => validate_worker_hello(body),
        WireMessageKind::Execute => validate_execute(body),
        WireMessageKind::Cancel => validate_cancel(body),
        WireMessageKind::Chunk => validate_chunk(body),
        WireMessageKind::Output => validate_output(body),
        WireMessageKind::Terminal => validate_terminal(body),
    }
}

fn validate_client_hello(body: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(
        body,
        &[
            "supportedVersions",
            "requestedCapability",
            "profile",
            "requiredLanes",
            "artifact",
        ],
        "FDIR-PROTOCOL-CLIENT-HELLO-FIELD",
    )?;
    let versions = required_string_set(body, "supportedVersions")?;
    if !versions.contains(PROTOCOL_VERSION) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-VERSION-MISMATCH",
            "client hello does not offer the current protocol version",
        ));
    }
    required_non_empty_string(body, "requestedCapability")?;
    required_non_empty_string(body, "profile")?;
    required_lane_set(body, "requiredLanes")?;
    validate_artifact_object(required_object(body, "artifact")?)
}

fn validate_worker_hello(body: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(
        body,
        &[
            "selectedVersion",
            "manifestDigest",
            "capabilities",
            "lanes",
            "qualificationState",
            "productionReady",
        ],
        "FDIR-PROTOCOL-WORKER-HELLO-FIELD",
    )?;
    if required_string(body, "selectedVersion")? != PROTOCOL_VERSION {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-VERSION-MISMATCH",
            "worker selected an unsupported protocol version",
        ));
    }
    required_digest(body, "manifestDigest")?;
    required_non_empty_string_set(body, "capabilities")?;
    required_lane_set(body, "lanes")?;
    let qualification = QualificationState::parse(required_string(body, "qualificationState")?)?;
    let production_ready = required_bool(body, "productionReady")?;
    if production_ready && qualification != QualificationState::ProductionQualified {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-FALSE-PRODUCTION-CLAIM",
            "worker hello cannot advertise production-ready without production qualification",
        ));
    }
    Ok(())
}

fn validate_execute(body: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(
        body,
        &[
            "manifestDigest",
            "artifact",
            "capability",
            "profile",
            "lanes",
            "budget",
            "configurationDigest",
            "contextDigest",
            "replayKey",
        ],
        "FDIR-PROTOCOL-EXECUTE-FIELD",
    )?;
    required_digest(body, "manifestDigest")?;
    validate_artifact_object(required_object(body, "artifact")?)?;
    required_non_empty_string(body, "capability")?;
    required_non_empty_string(body, "profile")?;
    required_lane_set(body, "lanes")?;
    validate_budget_object(required_object(body, "budget")?)?;
    required_digest(body, "configurationDigest")?;
    if let Some(value) = body.get("contextDigest") {
        if !matches!(value, CanonicalValue::Null) {
            parse_digest_value(value, "contextDigest")?;
        }
    } else {
        return Err(missing_field("contextDigest"));
    }
    required_non_empty_string(body, "replayKey")?;
    Ok(())
}

fn validate_cancel(body: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(body, &["reason"], "FDIR-PROTOCOL-CANCEL-FIELD")?;
    required_non_empty_string(body, "reason")?;
    Ok(())
}

fn validate_chunk(body: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(
        body,
        &[
            "lane",
            "chunkSequence",
            "byteLength",
            "final",
            "payloadDigest",
        ],
        "FDIR-PROTOCOL-CHUNK-FIELD",
    )?;
    ProtocolLane::parse(required_string(body, "lane")?)?;
    required_u64(body, "chunkSequence")?;
    if required_u64(body, "byteLength")? == 0 {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-CHUNK-LENGTH",
            "chunk byte length must be positive",
        ));
    }
    required_bool(body, "final")?;
    required_digest(body, "payloadDigest")?;
    Ok(())
}

fn validate_output(body: &ObjectValue) -> Result<(), ProtocolError> {
    let lane = ProtocolLane::parse(required_string(body, "lane")?)?;
    match lane {
        ProtocolLane::NativeSubstrateCensus => {
            reject_unknown_fields(
                body,
                &["lane", "inventoryItemId", "selector", "evidenceDigest"],
                "FDIR-PROTOCOL-NATIVE-OUTPUT-FIELD",
            )?;
            required_non_empty_string(body, "inventoryItemId")?;
            required_value(body, "selector")?;
            required_digest(body, "evidenceDigest")?;
        }
        ProtocolLane::SemanticHelper => {
            reject_unknown_fields(
                body,
                &["lane", "candidateId", "sourceOccurrenceIds", "value"],
                "FDIR-PROTOCOL-SEMANTIC-OUTPUT-FIELD",
            )?;
            required_non_empty_string(body, "candidateId")?;
            required_non_empty_string_array(body, "sourceOccurrenceIds")?;
            required_value(body, "value")?;
        }
        ProtocolLane::RendererObservation => {
            reject_unknown_fields(
                body,
                &[
                    "lane",
                    "observationId",
                    "sourceOccurrenceIds",
                    "rendererVersion",
                    "value",
                ],
                "FDIR-PROTOCOL-RENDERER-OUTPUT-FIELD",
            )?;
            required_non_empty_string(body, "observationId")?;
            required_non_empty_string_array(body, "sourceOccurrenceIds")?;
            let version = required_non_empty_string(body, "rendererVersion")?;
            if !is_exact_version(version) {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-RENDERER-VERSION",
                    "renderer version must be exact and immutable",
                ));
            }
            required_value(body, "value")?;
        }
        ProtocolLane::OcrInferenceObservation => {
            reject_unknown_fields(
                body,
                &[
                    "lane",
                    "observationId",
                    "sourceOccurrenceIds",
                    "method",
                    "confidenceMillionths",
                    "value",
                ],
                "FDIR-PROTOCOL-OCR-OUTPUT-FIELD",
            )?;
            required_non_empty_string(body, "observationId")?;
            required_non_empty_string_array(body, "sourceOccurrenceIds")?;
            required_non_empty_string(body, "method")?;
            let confidence = required_u64(body, "confidenceMillionths")?;
            if confidence > 1_000_000 {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-OCR-CONFIDENCE",
                    "confidence exceeds 1,000,000 millionths",
                ));
            }
            required_value(body, "value")?;
        }
        ProtocolLane::StorageCodec => {
            reject_unknown_fields(
                body,
                &["lane", "objectDigest", "byteLength"],
                "FDIR-PROTOCOL-STORAGE-OUTPUT-FIELD",
            )?;
            required_digest(body, "objectDigest")?;
            if required_u64(body, "byteLength")? == 0 {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-STORAGE-LENGTH",
                    "storage output byte length must be positive",
                ));
            }
        }
    }
    Ok(())
}

fn validate_terminal(body: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(
        body,
        &[
            "outcome",
            "artifactDigest",
            "manifestDigest",
            "outputComplete",
            "retryable",
            "usage",
            "provenance",
            "diagnosticCode",
        ],
        "FDIR-PROTOCOL-TERMINAL-FIELD",
    )?;
    let outcome = WorkerOutcome::parse(required_string(body, "outcome")?)?;
    required_digest(body, "artifactDigest")?;
    required_digest(body, "manifestDigest")?;
    let output_complete = required_bool(body, "outputComplete")?;
    let retryable = required_bool(body, "retryable")?;
    validate_usage_object(required_object(body, "usage")?)?;
    validate_provenance_object(required_object(body, "provenance")?)?;
    required_non_empty_string(body, "diagnosticCode")?;
    if outcome == WorkerOutcome::Complete && (!output_complete || retryable) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-FALSE-COMPLETE",
            "complete terminal message must mark output complete and non-retryable",
        ));
    }
    if outcome == WorkerOutcome::TruncatedOutput && output_complete {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-TRUNCATED-COMPLETE",
            "truncated terminal message cannot mark output complete",
        ));
    }
    Ok(())
}

fn validate_artifact_object(object: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(
        object,
        &["handle", "digest", "byteLength", "mediaType"],
        "FDIR-PROTOCOL-ARTIFACT-FIELD",
    )?;
    let handle = required_non_empty_string(object, "handle")?;
    if !is_opaque_handle(handle) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-ARTIFACT-HANDLE",
            "wire artifact handle is path-like or otherwise non-opaque",
        ));
    }
    required_digest(object, "digest")?;
    if required_u64(object, "byteLength")? == 0 {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-ARTIFACT-LENGTH",
            "wire artifact byte length must be positive",
        ));
    }
    required_non_empty_string(object, "mediaType")?;
    Ok(())
}

fn validate_budget_object(object: &ObjectValue) -> Result<(), ProtocolError> {
    let fields = [
        "maxCpuMillis",
        "maxMemoryBytes",
        "maxOutputBytes",
        "maxObjects",
        "maxRecursionDepth",
        "maxDecompressionRatio",
        "maxWallClockMillis",
        "maxTemporaryStorageBytes",
        "maxChunkBytes",
        "maxInFlightChunks",
    ];
    reject_unknown_fields(object, &fields, "FDIR-PROTOCOL-BUDGET-FIELD")?;
    for field in fields {
        if required_u64(object, field)? == 0 {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-BUDGET-NONPOSITIVE",
                format!("budget field {field} must be positive"),
            ));
        }
    }
    Ok(())
}

fn validate_usage_object(object: &ObjectValue) -> Result<(), ProtocolError> {
    let fields = [
        "cpuMillis",
        "peakMemoryBytes",
        "outputBytes",
        "objectCount",
        "recursionDepth",
        "compressedInputBytes",
        "decompressedBytes",
        "wallClockMillis",
        "temporaryStorageBytes",
        "emittedChunks",
    ];
    reject_unknown_fields(object, &fields, "FDIR-PROTOCOL-USAGE-FIELD")?;
    for field in fields {
        required_u64(object, field)?;
    }
    Ok(())
}

fn validate_provenance_object(object: &ObjectValue) -> Result<(), ProtocolError> {
    reject_unknown_fields(
        object,
        &[
            "workerId",
            "workerVersion",
            "buildDigest",
            "configurationDigest",
            "platform",
            "dependencyIds",
        ],
        "FDIR-PROTOCOL-PROVENANCE-FIELD",
    )?;
    required_non_empty_string(object, "workerId")?;
    let version = required_non_empty_string(object, "workerVersion")?;
    if !is_exact_version(version) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-PROVENANCE-WORKER-VERSION",
            "wire provenance worker version must be exact",
        ));
    }
    required_digest(object, "buildDigest")?;
    required_digest(object, "configurationDigest")?;
    required_non_empty_string(object, "platform")?;
    required_non_empty_string_array(object, "dependencyIds")?;
    Ok(())
}

fn validate_critical_fields(
    critical: &BTreeSet<String>,
    envelope: &ObjectValue,
    body: &ObjectValue,
    kind: WireMessageKind,
) -> Result<(), ProtocolError> {
    let allowed_body = allowed_body_fields(kind);
    for field in critical {
        if let Some(body_field) = field.strip_prefix("body.") {
            if !allowed_body.contains(&body_field) || !body.contains_key(body_field) {
                return Err(ProtocolError::new(
                    "FDIR-PROTOCOL-UNKNOWN-CRITICAL-FIELD",
                    format!("unknown or absent critical body field {field:?}"),
                ));
            }
        } else if ![
            "schema",
            "protocolVersion",
            "kind",
            "sessionId",
            "requestId",
            "sequence",
            "critical",
            "body",
        ]
        .contains(&field.as_str())
            || !envelope.contains_key(field)
        {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-UNKNOWN-CRITICAL-FIELD",
                format!("unknown or absent critical envelope field {field:?}"),
            ));
        }
    }
    Ok(())
}

fn allowed_body_fields(kind: WireMessageKind) -> BTreeSet<&'static str> {
    let fields: &[&str] = match kind {
        WireMessageKind::ClientHello => &[
            "supportedVersions",
            "requestedCapability",
            "profile",
            "requiredLanes",
            "artifact",
        ],
        WireMessageKind::WorkerHello => &[
            "selectedVersion",
            "manifestDigest",
            "capabilities",
            "lanes",
            "qualificationState",
            "productionReady",
        ],
        WireMessageKind::Execute => &[
            "manifestDigest",
            "artifact",
            "capability",
            "profile",
            "lanes",
            "budget",
            "configurationDigest",
            "contextDigest",
            "replayKey",
        ],
        WireMessageKind::Cancel => &["reason"],
        WireMessageKind::Chunk => &[
            "lane",
            "chunkSequence",
            "byteLength",
            "final",
            "payloadDigest",
        ],
        WireMessageKind::Output => &[
            "lane",
            "inventoryItemId",
            "selector",
            "evidenceDigest",
            "candidateId",
            "sourceOccurrenceIds",
            "value",
            "observationId",
            "rendererVersion",
            "method",
            "confidenceMillionths",
            "objectDigest",
            "byteLength",
        ],
        WireMessageKind::Terminal => &[
            "outcome",
            "artifactDigest",
            "manifestDigest",
            "outputComplete",
            "retryable",
            "usage",
            "provenance",
            "diagnosticCode",
        ],
    };
    fields.iter().copied().collect()
}

fn validate_usage(budget: ResourceBudget, usage: ResourceUsage) -> Result<(), ProtocolError> {
    let checks = [
        (
            usage.cpu_millis > budget.max_cpu_millis,
            BudgetDimension::Cpu,
        ),
        (
            usage.peak_memory_bytes > budget.max_memory_bytes,
            BudgetDimension::Memory,
        ),
        (
            usage.output_bytes > budget.max_output_bytes,
            BudgetDimension::Output,
        ),
        (
            usage.object_count > budget.max_objects,
            BudgetDimension::ObjectCount,
        ),
        (
            usage.recursion_depth > budget.max_recursion_depth,
            BudgetDimension::Recursion,
        ),
        (
            usage.wall_clock_millis > budget.max_wall_clock_millis,
            BudgetDimension::WallClock,
        ),
        (
            usage.temporary_storage_bytes > budget.max_temporary_storage_bytes,
            BudgetDimension::TemporaryStorage,
        ),
    ];
    for (exceeded, dimension) in checks {
        if exceeded {
            return Err(resource_error(dimension));
        }
    }
    if usage.decompressed_bytes > 0
        && (usage.compressed_input_bytes == 0
            || usage.decompressed_bytes
                > usage
                    .compressed_input_bytes
                    .saturating_mul(u64::from(budget.max_decompression_ratio)))
    {
        return Err(resource_error(BudgetDimension::Decompression));
    }
    Ok(())
}

fn resource_error(dimension: BudgetDimension) -> ProtocolError {
    ProtocolError::new(
        "FDIR-PROTOCOL-RESOURCE-LIMIT",
        format!("{} resource budget exceeded", dimension.as_str()),
    )
}

fn identity_error(subject: &str) -> ProtocolError {
    ProtocolError::new(
        "FDIR-PROTOCOL-IDENTITY-MISMATCH",
        format!("{subject} does not match the negotiated identity"),
    )
}

fn require_source_occurrences(values: &[String]) -> Result<(), ProtocolError> {
    if values.is_empty() || values.iter().any(String::is_empty) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-SOURCE-OCCURRENCE-REQUIRED",
            "derived evidence lanes require non-empty source occurrence links",
        ));
    }
    Ok(())
}

fn ensure_non_empty(value: &str, code: &'static str, subject: &str) -> Result<(), ProtocolError> {
    if value.is_empty() || value.chars().any(char::is_control) {
        return Err(ProtocolError::new(
            code,
            format!("{subject} must be non-empty and contain no control characters"),
        ));
    }
    Ok(())
}

fn ensure_manifest_id(value: &str, subject: &str) -> Result<(), ProtocolError> {
    ensure_non_empty(value, "FDIR-PROTOCOL-MANIFEST-ID", subject)?;
    if !value.bytes().all(|byte| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
    }) || !value
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_alphanumeric)
    {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-MANIFEST-ID",
            format!("{subject} must use lowercase manifest identifier syntax"),
        ));
    }
    Ok(())
}

fn ensure_non_empty_values(
    values: &BTreeSet<String>,
    code: &'static str,
    subject: &str,
) -> Result<(), ProtocolError> {
    if values.iter().any(|value| value.is_empty()) {
        return Err(ProtocolError::new(
            code,
            format!("{subject} cannot be empty"),
        ));
    }
    Ok(())
}

fn ensure_non_empty_slice(
    values: &[String],
    code: &'static str,
    subject: &str,
) -> Result<(), ProtocolError> {
    if values.iter().any(String::is_empty) {
        return Err(ProtocolError::new(
            code,
            format!("{subject} cannot be empty"),
        ));
    }
    Ok(())
}

fn is_exact_version(value: &str) -> bool {
    if value.is_empty()
        || value.chars().any(char::is_whitespace)
        || value
            .chars()
            .any(|character| matches!(character, '*' | '^' | '~' | '<' | '>' | '|'))
    {
        return false;
    }
    let lowercase = value.to_ascii_lowercase();
    !matches!(
        lowercase.as_str(),
        "latest" | "main" | "master" | "head" | "stable" | "nightly"
    ) && !lowercase.ends_with(".x")
}

fn is_opaque_handle(value: &str) -> bool {
    value.starts_with("artifact:")
        && value.len() > "artifact:".len()
        && !value.contains('/')
        && !value.contains('\\')
        && !value.contains("..")
        && value
            .chars()
            .all(|character| !character.is_whitespace() && !character.is_control())
}

fn length_prefixed<'a>(values: impl IntoIterator<Item = &'a str>) -> String {
    let mut output = String::new();
    for value in values {
        output.push_str(&value.len().to_string());
        output.push(':');
        output.push_str(value);
        output.push('|');
    }
    output
}

fn reject_unknown_fields(
    object: &ObjectValue,
    allowed: &[&str],
    code: &'static str,
) -> Result<(), ProtocolError> {
    if let Some(field) = object
        .keys()
        .find(|field| !allowed.contains(&field.as_str()))
    {
        return Err(ProtocolError::new(
            code,
            format!("unknown or disallowed field {field:?}"),
        ));
    }
    Ok(())
}

fn required_value<'a>(
    object: &'a ObjectValue,
    field: &str,
) -> Result<&'a CanonicalValue, ProtocolError> {
    object.get(field).ok_or_else(|| missing_field(field))
}

fn required_string<'a>(object: &'a ObjectValue, field: &str) -> Result<&'a str, ProtocolError> {
    required_value(object, field)?.as_str().ok_or_else(|| {
        ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-TYPE",
            format!("field {field:?} must be a string"),
        )
    })
}

fn required_non_empty_string<'a>(
    object: &'a ObjectValue,
    field: &str,
) -> Result<&'a str, ProtocolError> {
    let value = required_string(object, field)?;
    ensure_non_empty(
        value,
        "FDIR-PROTOCOL-FIELD-VALUE",
        &format!("field {field:?}"),
    )?;
    Ok(value)
}

fn required_bool(object: &ObjectValue, field: &str) -> Result<bool, ProtocolError> {
    required_value(object, field)?.as_bool().ok_or_else(|| {
        ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-TYPE",
            format!("field {field:?} must be a boolean"),
        )
    })
}

fn required_u64(object: &ObjectValue, field: &str) -> Result<u64, ProtocolError> {
    required_value(object, field)?
        .as_number()
        .and_then(fdir_core::JsonNumber::as_u64)
        .ok_or_else(|| {
            ProtocolError::new(
                "FDIR-PROTOCOL-FIELD-TYPE",
                format!("field {field:?} must be a non-negative integer"),
            )
        })
}

fn required_object<'a>(
    object: &'a ObjectValue,
    field: &str,
) -> Result<&'a ObjectValue, ProtocolError> {
    required_value(object, field)?.as_object().ok_or_else(|| {
        ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-TYPE",
            format!("field {field:?} must be an object"),
        )
    })
}

fn required_string_set(
    object: &ObjectValue,
    field: &str,
) -> Result<BTreeSet<String>, ProtocolError> {
    let values = required_value(object, field)?.as_array().ok_or_else(|| {
        ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-TYPE",
            format!("field {field:?} must be an array"),
        )
    })?;
    let mut output = BTreeSet::new();
    for value in values {
        let text = value.as_str().ok_or_else(|| {
            ProtocolError::new(
                "FDIR-PROTOCOL-FIELD-TYPE",
                format!("field {field:?} must contain only strings"),
            )
        })?;
        if !output.insert(text.to_owned()) {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-DUPLICATE-VALUE",
                format!("field {field:?} contains duplicate value {text:?}"),
            ));
        }
    }
    Ok(output)
}

fn required_non_empty_string_set(
    object: &ObjectValue,
    field: &str,
) -> Result<BTreeSet<String>, ProtocolError> {
    let values = required_string_set(object, field)?;
    if values.is_empty() || values.iter().any(String::is_empty) {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-VALUE",
            format!("field {field:?} requires non-empty unique strings"),
        ));
    }
    Ok(values)
}

fn required_non_empty_string_array(
    object: &ObjectValue,
    field: &str,
) -> Result<Vec<String>, ProtocolError> {
    let values = required_value(object, field)?.as_array().ok_or_else(|| {
        ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-TYPE",
            format!("field {field:?} must be an array"),
        )
    })?;
    let mut output = Vec::new();
    for value in values {
        let text = value.as_str().ok_or_else(|| {
            ProtocolError::new(
                "FDIR-PROTOCOL-FIELD-TYPE",
                format!("field {field:?} must contain only strings"),
            )
        })?;
        if text.is_empty() {
            return Err(ProtocolError::new(
                "FDIR-PROTOCOL-FIELD-VALUE",
                format!("field {field:?} cannot contain an empty string"),
            ));
        }
        output.push(text.to_owned());
    }
    if output.is_empty() {
        return Err(ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-VALUE",
            format!("field {field:?} cannot be empty"),
        ));
    }
    Ok(output)
}

fn required_lane_set(
    object: &ObjectValue,
    field: &str,
) -> Result<BTreeSet<ProtocolLane>, ProtocolError> {
    let strings = required_non_empty_string_set(object, field)?;
    strings
        .iter()
        .map(|value| ProtocolLane::parse(value))
        .collect()
}

fn required_digest(object: &ObjectValue, field: &str) -> Result<Digest, ProtocolError> {
    parse_digest_value(required_value(object, field)?, field)
}

fn parse_digest_value(value: &CanonicalValue, field: &str) -> Result<Digest, ProtocolError> {
    let text = value.as_str().ok_or_else(|| {
        ProtocolError::new(
            "FDIR-PROTOCOL-FIELD-TYPE",
            format!("field {field:?} must be a digest string"),
        )
    })?;
    Digest::new(text.to_owned()).map_err(|error| {
        ProtocolError::new(
            "FDIR-PROTOCOL-DIGEST",
            format!("invalid digest in field {field:?}: {error}"),
        )
    })
}

fn missing_field(field: &str) -> ProtocolError {
    ProtocolError::new(
        "FDIR-PROTOCOL-MISSING-FIELD",
        format!("required field {field:?} is missing"),
    )
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use fdir_core::{CanonicalValue, Digest};

    use super::{
        ArtifactHandle, BudgetTracker, CapabilityDeclaration, DependencyDeclaration,
        ExecutionRequest, LaneOutput, NativeSubstrateReceipt, NegotiationRequest, NetworkPolicy,
        ProcessBoundary, ProtocolLane, ProtocolSession, QualificationState, ReplayIdentity,
        ResourceBudget, ResourceUsage, SessionState, TerminalReceipt, WireEnvelope,
        WorkerFailureSignals, WorkerManifest, WorkerOutcome, WorkerProvenance,
        classify_worker_failure, negotiate,
    };

    const DIGEST_A: &str =
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const DIGEST_B: &str =
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const DIGEST_C: &str =
        "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

    fn digest(value: &str) -> Result<Digest, Box<dyn std::error::Error>> {
        Digest::new(value.to_owned()).map_err(Into::into)
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

    fn manifest() -> Result<WorkerManifest, Box<dyn std::error::Error>> {
        Ok(WorkerManifest {
            schema: "fdir/adapter-worker-manifest/1".to_owned(),
            id: "mock-python-worker".to_owned(),
            name: "Mock Python worker".to_owned(),
            version: "1.0.0".to_owned(),
            build_digest: digest(DIGEST_B)?,
            implementation_language: "Python".to_owned(),
            protocol_versions: BTreeSet::from(["1.0.0".to_owned()]),
            lanes: BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
            capabilities: vec![CapabilityDeclaration {
                id: "test-native-census".to_owned(),
                profiles: BTreeSet::from(["conformance".to_owned()]),
                lanes: BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
                qualification_state: QualificationState::AdmittedUnqualified,
            }],
            dependencies: vec![DependencyDeclaration {
                id: "python-runtime".to_owned(),
                version: "3.12.11".to_owned(),
                features: BTreeSet::new(),
                lanes: BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
                normalizations: Vec::new(),
                unavailable_source_distinctions: Vec::new(),
                unsafe_code: false,
                ffi: false,
                native_code: false,
                process_boundary: ProcessBoundary::IsolatedWorker,
            }],
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
        })
    }

    fn session_and_request()
    -> Result<(super::NegotiatedSession, ExecutionRequest), Box<dyn std::error::Error>> {
        let artifact = ArtifactHandle::new(
            "artifact:source-1",
            digest(DIGEST_A)?,
            12,
            "application/octet-stream",
        )?;
        let manifest = manifest()?;
        let session = negotiate(
            &manifest,
            digest(DIGEST_B)?,
            NegotiationRequest {
                supported_versions: BTreeSet::from(["1.0.0".to_owned()]),
                capability: "test-native-census".to_owned(),
                profile: "conformance".to_owned(),
                required_lanes: BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
                artifact: artifact.clone(),
                require_production_qualification: false,
            },
        )?;
        let replay_identity = ReplayIdentity {
            request_id: "request-1".to_owned(),
            artifact_digest: artifact.digest.clone(),
            manifest_digest: digest(DIGEST_B)?,
            worker_version: "1.0.0".to_owned(),
            configuration_digest: digest(DIGEST_C)?,
            context_digest: None,
            capability: "test-native-census".to_owned(),
            profile: "conformance".to_owned(),
            lanes: BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
        };
        let request = ExecutionRequest {
            session_key: session.session_key(),
            replay_key: replay_identity.key(),
            replay_identity,
            artifact,
            manifest_digest: digest(DIGEST_B)?,
            capability: "test-native-census".to_owned(),
            profile: "conformance".to_owned(),
            lanes: BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
            budget: budget(),
        };
        Ok((session, request))
    }

    #[test]
    fn manifest_and_negotiation_fail_closed() -> Result<(), Box<dyn std::error::Error>> {
        let mut invalid_manifest = manifest()?;
        invalid_manifest.process_boundary = ProcessBoundary::InProcess;
        assert_eq!(
            invalid_manifest.validate().map_err(|error| error.code()),
            Err("FDIR-PROTOCOL-WORKER-ISOLATION")
        );

        let manifest = manifest()?;
        let artifact = ArtifactHandle::new(
            "artifact:source-1",
            digest(DIGEST_A)?,
            12,
            "application/octet-stream",
        )?;
        let result = negotiate(
            &manifest,
            digest(DIGEST_B)?,
            NegotiationRequest {
                supported_versions: BTreeSet::from(["2.0.0".to_owned()]),
                capability: "test-native-census".to_owned(),
                profile: "conformance".to_owned(),
                required_lanes: BTreeSet::from([ProtocolLane::NativeSubstrateCensus]),
                artifact,
                require_production_qualification: false,
            },
        );
        assert_eq!(
            result.map_err(|error| error.code()),
            Err("FDIR-PROTOCOL-VERSION-MISMATCH")
        );
        Ok(())
    }

    #[test]
    fn session_enforces_identity_lane_stream_and_terminal_state()
    -> Result<(), Box<dyn std::error::Error>> {
        let (negotiated, request) = session_and_request()?;
        let mut session = ProtocolSession::start(negotiated, request.clone())?;
        let output = LaneOutput::NativeSubstrate(NativeSubstrateReceipt {
            inventory_item_id: "item-1".to_owned(),
            selector: CanonicalValue::String("bytes:0-12".to_owned()),
            evidence_digest: digest(DIGEST_A)?,
        });
        session.accept_output(0, 24, true, &output)?;
        session.acknowledge_output(0)?;
        let receipt = TerminalReceipt {
            request_id: "request-1".to_owned(),
            artifact_digest: digest(DIGEST_A)?,
            manifest_digest: digest(DIGEST_B)?,
            outcome: WorkerOutcome::Complete,
            output_complete: true,
            retryable: false,
            diagnostic_code: "FDIR-WORKER-COMPLETE".to_owned(),
            usage: session.tracker().usage(),
            provenance: WorkerProvenance {
                worker_id: "mock-python-worker".to_owned(),
                worker_version: "1.0.0".to_owned(),
                build_digest: digest(DIGEST_B)?,
                configuration_digest: digest(DIGEST_C)?,
                platform: "test".to_owned(),
                dependency_ids: vec!["python-runtime".to_owned()],
            },
        };
        session.finish(receipt)?;
        assert_eq!(session.state(), SessionState::Terminal);
        assert!(session.accept_output(1, 1, false, &output).is_err());

        let mut wrong = request;
        wrong.replay_key.push('x');
        let (negotiated, _) = session_and_request()?;
        assert_eq!(
            ProtocolSession::start(negotiated, wrong).map_err(|error| error.code()),
            Err("FDIR-PROTOCOL-REPLAY-KEY")
        );
        Ok(())
    }

    #[test]
    fn resource_and_backpressure_limits_are_explicit() -> Result<(), Box<dyn std::error::Error>> {
        let mut tracker = BudgetTracker::new(budget())?;
        tracker.admit_chunk(0, 100, false)?;
        tracker.admit_chunk(1, 100, false)?;
        assert_eq!(
            tracker
                .admit_chunk(2, 100, false)
                .map_err(|error| error.code()),
            Err("FDIR-PROTOCOL-RESOURCE-LIMIT")
        );
        tracker.acknowledge(0)?;
        tracker.admit_chunk(2, 100, true)?;
        let usage = ResourceUsage {
            compressed_input_bytes: 1,
            decompressed_bytes: 21,
            ..tracker.usage()
        };
        assert_eq!(
            tracker.observe(usage).map_err(|error| error.code()),
            Err("FDIR-PROTOCOL-RESOURCE-LIMIT")
        );
        Ok(())
    }

    #[test]
    fn strict_wire_decoder_rejects_unknown_version_field_lane_and_identity_shape() {
        let valid = include_str!("../../../fixtures/adapter-protocol/valid-output.json");
        let envelope = WireEnvelope::decode_json(valid);
        assert!(envelope.is_ok());
        assert_eq!(
            envelope.and_then(|value| value.lane()),
            Ok(Some(ProtocolLane::NativeSubstrateCensus))
        );
        for (input, code) in [
            (
                include_str!("../../../fixtures/adapter-protocol/invalid-version.json"),
                "FDIR-PROTOCOL-VERSION-MISMATCH",
            ),
            (
                include_str!("../../../fixtures/adapter-protocol/unknown-critical-field.json"),
                "FDIR-PROTOCOL-UNKNOWN-CRITICAL-FIELD",
            ),
            (
                include_str!("../../../fixtures/adapter-protocol/lane-substitution.json"),
                "FDIR-PROTOCOL-SEMANTIC-OUTPUT-FIELD",
            ),
            (
                include_str!("../../../fixtures/adapter-protocol/path-artifact.json"),
                "FDIR-PROTOCOL-ARTIFACT-HANDLE",
            ),
        ] {
            let result = WireEnvelope::decode_json(input);
            assert_eq!(result.map_err(|error| error.code()), Err(code));
        }
    }

    #[test]
    fn every_operational_failure_remains_distinct() {
        let names = WorkerOutcome::ALL
            .into_iter()
            .map(WorkerOutcome::as_str)
            .collect::<BTreeSet<_>>();
        assert_eq!(names.len(), WorkerOutcome::ALL.len());
        assert_eq!(
            classify_worker_failure(WorkerFailureSignals::default()),
            WorkerOutcome::WorkerCrash
        );
        assert_eq!(
            classify_worker_failure(WorkerFailureSignals {
                timed_out: true,
                ..WorkerFailureSignals::default()
            }),
            WorkerOutcome::TimedOut
        );
        assert_eq!(
            classify_worker_failure(WorkerFailureSignals {
                cancelled: true,
                ..WorkerFailureSignals::default()
            }),
            WorkerOutcome::Cancelled
        );
    }
}
