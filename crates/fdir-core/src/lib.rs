#![forbid(unsafe_code)]
//! Format-neutral product foundation shared by every trusted Rust crate.

use std::error::Error;
use std::fmt::{self, Display, Formatter};

/// Stable command failure classes. Complete success is represented separately.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FailureClass {
    /// Command-line syntax or invocation error.
    Usage,
    /// Invalid configuration or input contract.
    Validation,
    /// Requested capability is not implemented or supported.
    Unsupported,
    /// Valid output exists but the requested operation is incomplete.
    Partial,
    /// Policy denied the operation.
    Policy,
    /// A declared resource bound was reached.
    ResourceLimit,
    /// The operation was cancelled.
    Cancelled,
    /// An unexpected implementation failure occurred.
    Internal,
}

impl FailureClass {
    /// All failure classes in stable presentation order.
    pub const ALL: [Self; 8] = [
        Self::Usage,
        Self::Validation,
        Self::Unsupported,
        Self::Partial,
        Self::Policy,
        Self::ResourceLimit,
        Self::Cancelled,
        Self::Internal,
    ];

    /// Stable machine-readable class name.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Usage => "usage",
            Self::Validation => "validation",
            Self::Unsupported => "unsupported",
            Self::Partial => "partial",
            Self::Policy => "policy",
            Self::ResourceLimit => "resource-limit",
            Self::Cancelled => "cancelled",
            Self::Internal => "internal",
        }
    }

    /// Stable process exit code.
    #[must_use]
    pub const fn exit_code(self) -> u8 {
        match self {
            Self::Usage => 2,
            Self::Validation => 3,
            Self::Unsupported => 4,
            Self::Partial => 5,
            Self::Policy => 6,
            Self::ResourceLimit => 7,
            Self::Cancelled => 8,
            Self::Internal => 70,
        }
    }
}

/// Structured command failure that cannot be confused with successful output.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandFailure {
    /// Stable failure class.
    pub class: FailureClass,
    /// Stable diagnostic code.
    pub code: &'static str,
    /// Human-readable explanation without source content or credentials.
    pub message: String,
}

impl CommandFailure {
    /// Build a structured command failure.
    pub fn new(class: FailureClass, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            class,
            code,
            message: message.into(),
        }
    }

    /// Render deterministic JSON without adding a serialization dependency.
    #[must_use]
    pub fn to_json(&self) -> String {
        format!(
            "{{\"status\":\"failed\",\"class\":{},\"code\":{},\"message\":{},\"productionReady\":false}}",
            json_quote(self.class.as_str()),
            json_quote(self.code),
            json_quote(&self.message),
        )
    }
}

impl Display for CommandFailure {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} [{}]: {}",
            self.class.as_str(),
            self.code,
            self.message
        )
    }
}

impl Error for CommandFailure {}

/// Explicit metadata for one capability boundary and its qualification state.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CapabilityStatus {
    /// Stable capability identifier.
    pub id: &'static str,
    /// Owning roadmap issue.
    pub owning_issue: u32,
    /// Whether the capability accepts work.
    pub available: bool,
    /// Whether any production qualification exists.
    pub production_ready: bool,
}

impl CapabilityStatus {
    /// Construct an explicitly unavailable capability boundary.
    #[must_use]
    pub const fn unavailable(id: &'static str, owning_issue: u32) -> Self {
        Self {
            id,
            owning_issue,
            available: false,
            production_ready: false,
        }
    }

    /// Construct an implemented capability that still carries no production qualification.
    #[must_use]
    pub const fn implemented_unqualified(id: &'static str, owning_issue: u32) -> Self {
        Self {
            id,
            owning_issue,
            available: true,
            production_ready: false,
        }
    }
}

/// Build and protocol metadata exposed by the CLI and public API.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BuildMetadata {
    /// Product crate version.
    pub product_version: &'static str,
    /// Source revision embedded by the deterministic build script.
    pub source_revision: &'static str,
    /// Frozen FDIR logical-model identifier.
    pub model_id: &'static str,
    /// Frozen FDIR logical-model version.
    pub model_version: &'static str,
    /// Adapter protocol development version.
    pub protocol_version: &'static str,
    /// Foundation builds are never production-ready.
    pub production_ready: bool,
}

impl BuildMetadata {
    /// Construct metadata while retaining model authority in `fdir-contract`.
    #[must_use]
    pub const fn foundation(
        product_version: &'static str,
        source_revision: &'static str,
        protocol_version: &'static str,
    ) -> Self {
        Self {
            product_version,
            source_revision,
            model_id: fdir_contract::MODEL_ID,
            model_version: fdir_contract::MODEL_VERSION,
            protocol_version,
            production_ready: false,
        }
    }
}

/// Quote a string as deterministic JSON.
#[must_use]
pub fn json_quote(value: &str) -> String {
    let mut output = String::with_capacity(value.len() + 2);
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value.is_control() => {
                output.push_str(&format!("\\u{:04x}", u32::from(value)));
            }
            value => output.push(value),
        }
    }
    output.push('"');
    output
}

mod foundation;

pub use fdir_contract::{CanonicalValue, Digest, JsonError, JsonNumber, ObjectValue, ValueError};
pub use foundation::{
    Budget, CapabilityRef, EvidenceLane, FoundationError, ProcessingResult, ProfileRef, Provenance,
    ResultState, StatusVector,
};

#[cfg(test)]
mod tests {
    use super::{CapabilityStatus, CommandFailure, FailureClass, json_quote};

    #[test]
    fn failure_exit_codes_are_distinct() {
        let mut codes: Vec<u8> = FailureClass::ALL
            .into_iter()
            .map(FailureClass::exit_code)
            .collect();
        codes.sort_unstable();
        codes.dedup();
        assert_eq!(codes.len(), FailureClass::ALL.len());
        assert!(!codes.contains(&0));
    }

    #[test]
    fn failure_json_is_structured_and_non_production() {
        let failure = CommandFailure::new(
            FailureClass::Unsupported,
            "FDIR-CAPABILITY-UNAVAILABLE",
            "not implemented",
        );
        let json = failure.to_json();
        assert!(json.contains("\"class\":\"unsupported\""));
        assert!(json.contains("\"productionReady\":false"));
    }

    #[test]
    fn json_quote_escapes_control_characters() {
        assert_eq!(json_quote("a\nb\"c"), "\"a\\nb\\\"c\"");
    }

    #[test]
    fn implemented_capabilities_remain_unqualified() {
        let capability = CapabilityStatus::implemented_unqualified("canonical-identity", 9);
        assert!(capability.available);
        assert!(!capability.production_ready);
    }
}
