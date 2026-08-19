#![forbid(unsafe_code)]

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt::{self, Display, Formatter};

use fdir_contract::{DiagnosticId, Identifier, OccurrenceId};

/// Explicit role assigned to a dependency or worker output.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum EvidenceLane {
    NativeSubstrate,
    SemanticCandidate,
    Renderer,
    OcrInference,
    StorageCodec,
}

impl EvidenceLane {
    /// Stable machine-readable evidence-lane name from the implementation policy.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NativeSubstrate => "native-substrate",
            Self::SemanticCandidate => "semantic-candidate",
            Self::Renderer => "renderer",
            Self::OcrInference => "ocr-inference",
            Self::StorageCodec => "storage-codec",
        }
    }
}

/// Operation state that never collapses a non-success condition into a boolean.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ResultState {
    Complete,
    Incomplete,
    Partial,
    Unsupported,
    Unresolved,
    Cancelled,
    Failed,
    Unreadable,
    ResourceLimited,
    PolicyExcluded,
}

impl ResultState {
    /// Every state in stable presentation order.
    pub const ALL: [Self; 10] = [
        Self::Complete,
        Self::Incomplete,
        Self::Partial,
        Self::Unsupported,
        Self::Unresolved,
        Self::Cancelled,
        Self::Failed,
        Self::Unreadable,
        Self::ResourceLimited,
        Self::PolicyExcluded,
    ];

    /// Stable machine-readable state name.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Complete => "complete",
            Self::Incomplete => "incomplete",
            Self::Partial => "partial",
            Self::Unsupported => "unsupported",
            Self::Unresolved => "unresolved",
            Self::Cancelled => "cancelled",
            Self::Failed => "failed",
            Self::Unreadable => "unreadable",
            Self::ResourceLimited => "resource-limited",
            Self::PolicyExcluded => "policy-excluded",
        }
    }

    /// Only the explicit complete state is complete.
    pub const fn is_complete(self) -> bool {
        matches!(self, Self::Complete)
    }
}

/// Strong reference to one capability registry entry.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct CapabilityRef(Identifier);

impl CapabilityRef {
    /// Construct a non-empty capability reference.
    pub fn new(value: impl Into<String>) -> Result<Self, fdir_contract::ValueError> {
        Identifier::new(value).map(Self)
    }

    /// Borrow the registry identifier.
    pub fn as_str(&self) -> &str {
        self.0.as_str()
    }
}

/// Strong reference to one guarantee or qualification profile.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct ProfileRef(Identifier);

impl ProfileRef {
    /// Construct a non-empty profile reference.
    pub fn new(value: impl Into<String>) -> Result<Self, fdir_contract::ValueError> {
        Identifier::new(value).map(Self)
    }

    /// Borrow the registry identifier.
    pub fn as_str(&self) -> &str {
        self.0.as_str()
    }
}

/// Declared resource ceiling. Absence means that dimension is not bounded here.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Budget {
    pub max_bytes: Option<u64>,
    pub max_items: Option<u64>,
    pub max_duration_millis: Option<u64>,
}

impl Budget {
    /// Construct a budget with at least one positive, explicit bound.
    pub fn new(
        max_bytes: Option<u64>,
        max_items: Option<u64>,
        max_duration_millis: Option<u64>,
    ) -> Result<Self, FoundationError> {
        let values = [max_bytes, max_items, max_duration_millis];
        if values.iter().all(Option::is_none) {
            return Err(FoundationError::new(
                "FDIR-BUDGET-EMPTY",
                "at least one budget dimension must be declared",
            ));
        }
        if values.into_iter().flatten().any(|value| value == 0) {
            return Err(FoundationError::new(
                "FDIR-BUDGET-NONPOSITIVE",
                "declared budget dimensions must be positive",
            ));
        }
        Ok(Self {
            max_bytes,
            max_items,
            max_duration_millis,
        })
    }
}

/// Provenance kept separate from assertion truth and linked to source occurrences.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Provenance {
    pub producer: String,
    pub version: String,
    pub lane: EvidenceLane,
    pub method: Option<String>,
    pub source_occurrence_ids: Vec<OccurrenceId>,
}

impl Provenance {
    /// Construct provenance while enforcing evidence-lane linkage requirements.
    pub fn new(
        producer: impl Into<String>,
        version: impl Into<String>,
        lane: EvidenceLane,
        method: Option<String>,
        source_occurrence_ids: Vec<OccurrenceId>,
    ) -> Result<Self, FoundationError> {
        let producer = producer.into();
        let version = version.into();
        if producer.is_empty() || version.is_empty() {
            return Err(FoundationError::new(
                "FDIR-PROVENANCE-IDENTITY",
                "producer and version must be non-empty",
            ));
        }
        if matches!(
            lane,
            EvidenceLane::SemanticCandidate | EvidenceLane::Renderer | EvidenceLane::OcrInference
        ) && source_occurrence_ids.is_empty()
        {
            return Err(FoundationError::new(
                "FDIR-PROVENANCE-SOURCE-REQUIRED",
                "derived evidence lanes must link to at least one source occurrence",
            ));
        }
        if lane == EvidenceLane::OcrInference && method.as_deref().is_none_or(str::is_empty) {
            return Err(FoundationError::new(
                "FDIR-PROVENANCE-METHOD-REQUIRED",
                "OCR or inference provenance must name its method",
            ));
        }
        Ok(Self {
            producer,
            version,
            lane,
            method,
            source_occurrence_ids,
        })
    }
}

/// Profile-scoped operation states without a lossy aggregate success flag.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct StatusVector {
    states: BTreeMap<ProfileRef, ResultState>,
}

impl StatusVector {
    /// Construct an empty vector that makes no completeness claim.
    pub const fn new() -> Self {
        Self {
            states: BTreeMap::new(),
        }
    }

    /// Set one profile state and return the previous state, if any.
    pub fn insert(&mut self, profile: ProfileRef, state: ResultState) -> Option<ResultState> {
        self.states.insert(profile, state)
    }

    /// Read one profile state.
    pub fn get(&self, profile: &ProfileRef) -> Option<ResultState> {
        self.states.get(profile).copied()
    }

    /// Number of explicitly represented profiles.
    pub fn len(&self) -> usize {
        self.states.len()
    }

    /// Whether no profile state has been recorded.
    pub fn is_empty(&self) -> bool {
        self.states.is_empty()
    }

    /// Completeness requires at least one profile and explicit completion of every profile.
    pub fn is_complete(&self) -> bool {
        !self.states.is_empty() && self.states.values().all(|state| state.is_complete())
    }

    /// Iterate in deterministic profile-reference order.
    pub fn iter(&self) -> impl Iterator<Item = (&ProfileRef, ResultState)> {
        self.states.iter().map(|(profile, state)| (profile, *state))
    }
}

/// Value plus an explicit state and diagnostic links.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProcessingResult<T> {
    pub state: ResultState,
    pub value: Option<T>,
    pub diagnostic_ids: Vec<DiagnosticId>,
}

impl<T> ProcessingResult<T> {
    /// Construct a complete result with an explicit value.
    pub fn complete(value: T) -> Self {
        Self {
            state: ResultState::Complete,
            value: Some(value),
            diagnostic_ids: Vec::new(),
        }
    }

    /// Construct any explicit state without coercing it to success or failure.
    pub fn new(
        state: ResultState,
        value: Option<T>,
        diagnostic_ids: Vec<DiagnosticId>,
    ) -> Result<Self, FoundationError> {
        if state == ResultState::Complete && value.is_none() {
            return Err(FoundationError::new(
                "FDIR-RESULT-COMPLETE-WITHOUT-VALUE",
                "a complete result must contain a value",
            ));
        }
        Ok(Self {
            state,
            value,
            diagnostic_ids,
        })
    }
}

/// Construction failure for neutral foundational types.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FoundationError {
    code: &'static str,
    message: String,
}

impl FoundationError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    /// Stable machine-readable failure code.
    pub const fn code(&self) -> &'static str {
        self.code
    }
}

impl Display for FoundationError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl Error for FoundationError {}

#[cfg(test)]
mod tests {
    use super::{
        Budget, EvidenceLane, ProcessingResult, ProfileRef, Provenance, ResultState, StatusVector,
    };
    use fdir_contract::OccurrenceId;

    #[test]
    fn every_non_success_state_remains_distinct() {
        let names: std::collections::BTreeSet<&str> = ResultState::ALL
            .into_iter()
            .map(ResultState::as_str)
            .collect();
        assert_eq!(names.len(), ResultState::ALL.len());
        assert_eq!(
            ResultState::ALL
                .iter()
                .filter(|state| state.is_complete())
                .count(),
            1
        );
    }

    #[test]
    fn status_vector_does_not_treat_empty_or_partial_as_complete()
    -> Result<(), Box<dyn std::error::Error>> {
        let mut vector = StatusVector::new();
        assert!(!vector.is_complete());
        vector.insert(ProfileRef::new("core")?, ResultState::Complete);
        vector.insert(ProfileRef::new("presentation")?, ResultState::Partial);
        assert!(!vector.is_complete());
        Ok(())
    }

    #[test]
    fn derived_provenance_requires_source_evidence() -> Result<(), Box<dyn std::error::Error>> {
        let missing = Provenance::new(
            "worker",
            "1.0",
            EvidenceLane::SemanticCandidate,
            None,
            Vec::new(),
        );
        assert!(missing.is_err());
        let occurrence = OccurrenceId::new("occurrence-1")?;
        let present = Provenance::new(
            "worker",
            "1.0",
            EvidenceLane::SemanticCandidate,
            Some("parse".to_owned()),
            vec![occurrence],
        );
        assert!(present.is_ok());
        Ok(())
    }

    #[test]
    fn complete_results_and_budgets_enforce_construction_invariants() {
        assert!(ProcessingResult::<u8>::new(ResultState::Complete, None, Vec::new()).is_err());
        assert!(Budget::new(None, None, None).is_err());
        assert!(Budget::new(Some(1), None, None).is_ok());
    }
}
