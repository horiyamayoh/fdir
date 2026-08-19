#![forbid(unsafe_code)]
//! Trusted orchestration metadata, configuration, telemetry, and redaction conventions.

use std::path::Path;

use fdir_core::{BuildMetadata, CapabilityStatus, CommandFailure, FailureClass};

/// Every capability boundary visible at the current implementation stage.
pub const CAPABILITIES: &[CapabilityStatus] = &[
    fdir_canonical::CAPABILITY,
    fdir_storage::CAPABILITY,
    fdir_adapter_sdk::CAPABILITY,
    fdir_accounting::CAPABILITY,
    fdir_adapters::CAPABILITY,
    fdir_semantics::PROJECTION,
    fdir_semantics::EQUIVALENCE,
    fdir_semantics::ALIGNMENT,
    fdir_semantics::LINEAGE,
];

/// Supported CLI output encodings.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum OutputFormat {
    /// Human-readable stable text.
    #[default]
    Text,
    /// Machine-readable JSON.
    Json,
}

impl OutputFormat {
    /// Stable configuration value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Json => "json",
        }
    }

    fn parse(value: &str) -> Result<Self, CommandFailure> {
        match value {
            "text" => Ok(Self::Text),
            "json" => Ok(Self::Json),
            _ => Err(validation_failure(
                "FDIR-CONFIG-OUTPUT",
                format!("output must be text or json, got {value:?}"),
            )),
        }
    }
}

/// Foundation logging levels. No source content is logged.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum LogLevel {
    /// Errors only.
    Error,
    /// Warnings and errors.
    Warn,
    /// Informational foundation events.
    #[default]
    Info,
    /// Developer diagnostics without source bytes or credentials.
    Debug,
}

impl LogLevel {
    /// Stable configuration value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Error => "error",
            Self::Warn => "warn",
            Self::Info => "info",
            Self::Debug => "debug",
        }
    }

    fn parse(value: &str) -> Result<Self, CommandFailure> {
        match value {
            "error" => Ok(Self::Error),
            "warn" => Ok(Self::Warn),
            "info" => Ok(Self::Info),
            "debug" => Ok(Self::Debug),
            _ => Err(validation_failure(
                "FDIR-CONFIG-LOG-LEVEL",
                format!("log_level must be error, warn, info, or debug, got {value:?}"),
            )),
        }
    }
}

/// Deterministic foundation configuration.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RuntimeConfig {
    /// Output encoding.
    pub output: OutputFormat,
    /// Foundation logging threshold.
    pub log_level: LogLevel,
    /// Whether filesystem paths are replaced with a redaction marker.
    pub redact_paths: bool,
    /// Seed used only by deterministic development/test facilities.
    pub deterministic_seed: u64,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            output: OutputFormat::Text,
            log_level: LogLevel::Info,
            redact_paths: true,
            deterministic_seed: 0,
        }
    }
}

/// Parse a deterministic `key=value` foundation configuration.
pub fn parse_config(text: &str) -> Result<RuntimeConfig, CommandFailure> {
    let mut config = RuntimeConfig::default();
    for (index, raw_line) in text.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((raw_key, raw_value)) = line.split_once('=') else {
            return Err(validation_failure(
                "FDIR-CONFIG-LINE",
                format!("configuration line {} must use key=value", index + 1),
            ));
        };
        let key = raw_key.trim();
        let value = raw_value.trim();
        match key {
            "output" => config.output = OutputFormat::parse(value)?,
            "log_level" => config.log_level = LogLevel::parse(value)?,
            "redact_paths" => config.redact_paths = parse_bool(value)?,
            "deterministic_seed" => {
                config.deterministic_seed = value.parse::<u64>().map_err(|_| {
                    validation_failure(
                        "FDIR-CONFIG-SEED",
                        format!("deterministic_seed must be an unsigned integer, got {value:?}"),
                    )
                })?;
            }
            _ => {
                return Err(validation_failure(
                    "FDIR-CONFIG-UNKNOWN-KEY",
                    format!("unknown configuration key {key:?} on line {}", index + 1),
                ));
            }
        }
    }
    Ok(config)
}

fn parse_bool(value: &str) -> Result<bool, CommandFailure> {
    match value {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(validation_failure(
            "FDIR-CONFIG-BOOLEAN",
            format!("boolean value must be true or false, got {value:?}"),
        )),
    }
}

fn validation_failure(code: &'static str, message: String) -> CommandFailure {
    CommandFailure::new(FailureClass::Validation, code, message)
}

/// Return build metadata without creating a production qualification claim.
#[must_use]
pub const fn build_metadata(
    product_version: &'static str,
    source_revision: &'static str,
) -> BuildMetadata {
    BuildMetadata::foundation(
        product_version,
        source_revision,
        fdir_adapter_sdk::PROTOCOL_VERSION,
    )
}

/// Redact a path according to the centralized telemetry convention.
#[must_use]
pub fn display_path(path: &Path, redact: bool) -> String {
    if redact {
        "<redacted-path>".to_owned()
    } else {
        path.to_string_lossy().into_owned()
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::{CAPABILITIES, LogLevel, OutputFormat, display_path, parse_config};

    #[test]
    fn implemented_capabilities_remain_explicitly_unqualified() {
        assert!(!CAPABILITIES.is_empty());
        assert!(
            CAPABILITIES
                .iter()
                .all(|capability| !capability.production_ready)
        );
        let available: Vec<&str> = CAPABILITIES
            .iter()
            .filter(|capability| capability.available)
            .map(|capability| capability.id)
            .collect();
        assert_eq!(available, vec!["canonical-identity"]);
    }

    #[test]
    fn deterministic_config_is_strict() {
        let config = parse_config(
            "output=json\nlog_level=debug\nredact_paths=false\ndeterministic_seed=42\n",
        );
        assert!(config.is_ok());
        let config = config.unwrap_or_default();
        assert_eq!(config.output, OutputFormat::Json);
        assert_eq!(config.log_level, LogLevel::Debug);
        assert!(!config.redact_paths);
        assert_eq!(config.deterministic_seed, 42);
        assert!(parse_config("unknown=value\n").is_err());
    }

    #[test]
    fn paths_are_redacted_by_default_convention() {
        assert_eq!(
            display_path(Path::new("/secret/source"), true),
            "<redacted-path>"
        );
    }
}
