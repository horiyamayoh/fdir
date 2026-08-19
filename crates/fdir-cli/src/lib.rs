#![forbid(unsafe_code)]
//! Stable command-line foundation for FDIR metadata and developer diagnostics.

use std::fs;
use std::path::{Path, PathBuf};

use fdir_coordinator::{
    CAPABILITIES, OutputFormat, RuntimeConfig, build_metadata, parse_config,
};
use fdir_core::{CommandFailure, FailureClass, json_quote};

/// Stable command help. No unavailable operation is advertised as successful.
pub const HELP: &str = "FDIR reference-product foundation\n\nUsage:\n  fdir [--config PATH] [--output text|json] <command>\n  fdir --help\n  fdir --version\n\nCommands:\n  metadata         Print build, model, protocol, and qualification metadata\n  capabilities     List unavailable product capability boundaries\n  status-codes     Print stable completion and failure exit semantics\n  validate-config  Validate the selected deterministic configuration\n\nGlobal options:\n  --config PATH       Read deterministic key=value configuration\n  --output FORMAT     Select text or JSON output\n  -h, --help          Print this help\n  -V, --version       Print stable version metadata\n";

const PRODUCT_VERSION: &str = env!("CARGO_PKG_VERSION");
const SOURCE_REVISION: &str = env!("FDIR_SOURCE_REVISION");

/// Complete or failed command status without collapsing partial and failure classes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExecutionStatus {
    /// Command completed fully.
    Complete,
    /// Command failed in the retained class.
    Failed(FailureClass),
}

/// Fully rendered command result with separated stdout and stderr channels.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Execution {
    /// Structured completion status.
    pub status: ExecutionStatus,
    /// Text for standard output.
    pub stdout: String,
    /// Text for standard error.
    pub stderr: String,
}

impl Execution {
    /// Stable process exit code derived from the structured status.
    #[must_use]
    pub const fn exit_code(&self) -> u8 {
        match self.status {
            ExecutionStatus::Complete => 0,
            ExecutionStatus::Failed(class) => class.exit_code(),
        }
    }

    fn complete(stdout: String) -> Self {
        Self {
            status: ExecutionStatus::Complete,
            stdout,
            stderr: String::new(),
        }
    }

    fn failed(failure: CommandFailure, output: OutputFormat) -> Self {
        let stderr = match output {
            OutputFormat::Text => failure.to_string(),
            OutputFormat::Json => failure.to_json(),
        };
        Self {
            status: ExecutionStatus::Failed(failure.class),
            stdout: String::new(),
            stderr,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CommandKind {
    Help,
    Version,
    Metadata,
    Capabilities,
    StatusCodes,
    ValidateConfig,
}

#[derive(Debug, Eq, PartialEq)]
struct Invocation {
    command: CommandKind,
    config_path: Option<PathBuf>,
    output_override: Option<OutputFormat>,
}

/// Execute one CLI invocation without writing to process-global streams.
#[must_use]
pub fn run(arguments: &[String]) -> Execution {
    let fallback_output = requested_output(arguments);
    match checked_run(arguments) {
        Ok(output) => Execution::complete(output),
        Err(failure) => Execution::failed(failure, fallback_output),
    }
}

fn checked_run(arguments: &[String]) -> Result<String, CommandFailure> {
    let invocation = parse_invocation(arguments)?;
    if invocation.command == CommandKind::Help {
        return Ok(HELP.to_owned());
    }
    if invocation.command == CommandKind::Version {
        return Ok(version_text());
    }

    let mut config = load_config(invocation.config_path.as_deref())?;
    if let Some(output) = invocation.output_override {
        config.output = output;
    }

    match invocation.command {
        CommandKind::Metadata => Ok(render_metadata(config.output)),
        CommandKind::Capabilities => Ok(render_capabilities(config.output)),
        CommandKind::StatusCodes => Ok(render_status_codes(config.output)),
        CommandKind::ValidateConfig => {
            if invocation.config_path.is_none() {
                return Err(CommandFailure::new(
                    FailureClass::Usage,
                    "FDIR-USAGE-CONFIG-REQUIRED",
                    "validate-config requires --config PATH",
                ));
            }
            Ok(render_config(config))
        }
        CommandKind::Help | CommandKind::Version => Err(CommandFailure::new(
            FailureClass::Internal,
            "FDIR-INTERNAL-DISPATCH",
            "internal command dispatch invariant failed",
        )),
    }
}

fn parse_invocation(arguments: &[String]) -> Result<Invocation, CommandFailure> {
    if arguments.is_empty() {
        return Err(usage_failure("a command is required; use --help"));
    }

    let mut command: Option<CommandKind> = None;
    let mut config_path: Option<PathBuf> = None;
    let mut output_override: Option<OutputFormat> = None;
    let mut index = 0;
    while index < arguments.len() {
        match arguments[index].as_str() {
            "-h" | "--help" => command = select_command(command, CommandKind::Help)?,
            "-V" | "--version" => command = select_command(command, CommandKind::Version)?,
            "--config" => {
                index += 1;
                let Some(value) = arguments.get(index) else {
                    return Err(usage_failure("--config requires a path"));
                };
                if config_path.replace(PathBuf::from(value)).is_some() {
                    return Err(usage_failure("--config may be supplied only once"));
                }
            }
            "--output" => {
                index += 1;
                let Some(value) = arguments.get(index) else {
                    return Err(usage_failure("--output requires text or json"));
                };
                let parsed = parse_output(value)?;
                if output_override.replace(parsed).is_some() {
                    return Err(usage_failure("--output may be supplied only once"));
                }
            }
            "metadata" => command = select_command(command, CommandKind::Metadata)?,
            "capabilities" => command = select_command(command, CommandKind::Capabilities)?,
            "status-codes" => command = select_command(command, CommandKind::StatusCodes)?,
            "validate-config" => command = select_command(command, CommandKind::ValidateConfig)?,
            value if value.starts_with('-') => {
                return Err(usage_failure(format!("unknown option {value:?}")));
            }
            value => return Err(usage_failure(format!("unknown command or operand {value:?}"))),
        }
        index += 1;
    }

    let Some(command) = command else {
        return Err(usage_failure("a command is required; use --help"));
    };
    Ok(Invocation {
        command,
        config_path,
        output_override,
    })
}

fn select_command(
    current: Option<CommandKind>,
    next: CommandKind,
) -> Result<Option<CommandKind>, CommandFailure> {
    match current {
        None => Ok(Some(next)),
        Some(existing) if existing == next => Err(usage_failure("command may be supplied only once")),
        Some(_) => Err(usage_failure("only one command may be selected")),
    }
}

fn parse_output(value: &str) -> Result<OutputFormat, CommandFailure> {
    match value {
        "text" => Ok(OutputFormat::Text),
        "json" => Ok(OutputFormat::Json),
        _ => Err(CommandFailure::new(
            FailureClass::Validation,
            "FDIR-OUTPUT-FORMAT",
            format!("output format must be text or json, got {value:?}"),
        )),
    }
}

fn requested_output(arguments: &[String]) -> OutputFormat {
    arguments
        .windows(2)
        .find_map(|pair| {
            if pair[0] == "--output" && pair[1] == "json" {
                Some(OutputFormat::Json)
            } else {
                None
            }
        })
        .unwrap_or(OutputFormat::Text)
}

fn load_config(path: Option<&Path>) -> Result<RuntimeConfig, CommandFailure> {
    let Some(path) = path else {
        return Ok(RuntimeConfig::default());
    };
    let text = fs::read_to_string(path).map_err(|error| {
        CommandFailure::new(
            FailureClass::Validation,
            "FDIR-CONFIG-READ",
            format!("configuration file could not be read: {error}"),
        )
    })?;
    parse_config(&text)
}

fn metadata() -> fdir_core::BuildMetadata {
    build_metadata(PRODUCT_VERSION, SOURCE_REVISION)
}

fn version_text() -> String {
    let metadata = metadata();
    format!(
        "fdir {} (source {}, model {}, protocol {}, production-ready false)",
        metadata.product_version,
        metadata.source_revision,
        metadata.model_version,
        metadata.protocol_version,
    )
}

fn render_metadata(output: OutputFormat) -> String {
    let metadata = metadata();
    match output {
        OutputFormat::Text => format!(
            "status: complete\nproduct-version: {}\nsource-revision: {}\nmodel-id: {}\nmodel-version: {}\nprotocol-version: {}\nproduction-ready: false",
            metadata.product_version,
            metadata.source_revision,
            metadata.model_id,
            metadata.model_version,
            metadata.protocol_version,
        ),
        OutputFormat::Json => format!(
            "{{\"status\":\"complete\",\"productVersion\":{},\"sourceRevision\":{},\"modelId\":{},\"modelVersion\":{},\"protocolVersion\":{},\"productionReady\":false}}",
            json_quote(metadata.product_version),
            json_quote(metadata.source_revision),
            json_quote(metadata.model_id),
            json_quote(metadata.model_version),
            json_quote(metadata.protocol_version),
        ),
    }
}

fn render_capabilities(output: OutputFormat) -> String {
    match output {
        OutputFormat::Text => CAPABILITIES
            .iter()
            .map(|capability| {
                format!(
                    "{}: unavailable (issue #{}, production-ready false)",
                    capability.id, capability.owning_issue
                )
            })
            .collect::<Vec<_>>()
            .join("\n"),
        OutputFormat::Json => {
            let values = CAPABILITIES
                .iter()
                .map(|capability| {
                    format!(
                        "{{\"id\":{},\"owningIssue\":{},\"available\":false,\"productionReady\":false}}",
                        json_quote(capability.id),
                        capability.owning_issue,
                    )
                })
                .collect::<Vec<_>>()
                .join(",");
            format!("{{\"status\":\"complete\",\"capabilities\":[{values}]}}")
        }
    }
}

fn render_status_codes(output: OutputFormat) -> String {
    match output {
        OutputFormat::Text => {
            let mut lines = vec!["complete: 0".to_owned()];
            lines.extend(
                FailureClass::ALL
                    .iter()
                    .map(|class| format!("{}: {}", class.as_str(), class.exit_code())),
            );
            lines.join("\n")
        }
        OutputFormat::Json => {
            let failures = FailureClass::ALL
                .iter()
                .map(|class| {
                    format!(
                        "{{\"class\":{},\"exitCode\":{}}}",
                        json_quote(class.as_str()),
                        class.exit_code(),
                    )
                })
                .collect::<Vec<_>>()
                .join(",");
            format!(
                "{{\"status\":\"complete\",\"completeExitCode\":0,\"failures\":[{failures}]}}"
            )
        }
    }
}

fn render_config(config: RuntimeConfig) -> String {
    match config.output {
        OutputFormat::Text => format!(
            "status: complete\noutput: {}\nlog-level: {}\nredact-paths: {}\ndeterministic-seed: {}\nproduction-ready: false",
            config.output.as_str(),
            config.log_level.as_str(),
            config.redact_paths,
            config.deterministic_seed,
        ),
        OutputFormat::Json => format!(
            "{{\"status\":\"complete\",\"output\":{},\"logLevel\":{},\"redactPaths\":{},\"deterministicSeed\":{},\"productionReady\":false}}",
            json_quote(config.output.as_str()),
            json_quote(config.log_level.as_str()),
            config.redact_paths,
            config.deterministic_seed,
        ),
    }
}

fn usage_failure(message: impl Into<String>) -> CommandFailure {
    CommandFailure::new(FailureClass::Usage, "FDIR-USAGE", message)
}

#[cfg(test)]
mod tests {
    use super::{ExecutionStatus, run};
    use fdir_core::FailureClass;

    fn arguments(values: &[&str]) -> Vec<String> {
        values.iter().map(ToString::to_string).collect()
    }

    #[test]
    fn help_and_metadata_are_complete() {
        let help = run(&arguments(&["--help"]));
        assert_eq!(help.status, ExecutionStatus::Complete);
        assert!(help.stdout.contains("status-codes"));

        let metadata = run(&arguments(&["metadata", "--output", "json"]));
        assert_eq!(metadata.exit_code(), 0);
        assert!(metadata.stdout.contains("\"modelVersion\":\"2.1.0\""));
        assert!(metadata.stdout.contains("\"productionReady\":false"));
    }

    #[test]
    fn unknown_commands_are_usage_failures() {
        let result = run(&arguments(&["convert"]));
        assert_eq!(result.status, ExecutionStatus::Failed(FailureClass::Usage));
        assert_eq!(result.exit_code(), 2);
        assert!(result.stdout.is_empty());
    }

    #[test]
    fn json_errors_retain_validation_class() {
        let result = run(&arguments(&["--output", "json", "--config"]));
        assert_eq!(
            result.status,
            ExecutionStatus::Failed(FailureClass::Usage)
        );
        assert!(result.stderr.contains("\"class\":\"usage\""));
    }
}
