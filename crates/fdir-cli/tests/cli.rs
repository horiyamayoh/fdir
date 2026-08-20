#![forbid(unsafe_code)]

use std::error::Error;
use std::ffi::OsStr;
use std::process::{Command, Output};

use fdir_test_support::TempStore;

fn invoke<I, S>(arguments: I) -> Result<Output, Box<dyn Error>>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    Command::new(env!("CARGO_BIN_EXE_fdir"))
        .args(arguments)
        .output()
        .map_err(Into::into)
}

#[test]
fn help_version_and_metadata_have_stable_foundation_shape() -> Result<(), Box<dyn Error>> {
    let help = invoke(["--help"])?;
    assert!(help.status.success());
    assert!(String::from_utf8(help.stdout)?.contains("FDIR reference-product foundation"));

    let version = invoke(["--version"])?;
    assert!(version.status.success());
    let version = String::from_utf8(version.stdout)?;
    assert!(version.starts_with("fdir 0.1.0 (source "));
    assert!(version.contains("model 2.1.0"));
    assert!(version.contains("production-ready false"));

    let metadata = invoke(["metadata", "--output", "json"])?;
    assert!(metadata.status.success());
    let metadata = String::from_utf8(metadata.stdout)?;
    assert!(metadata.contains("\"status\":\"complete\""));
    assert!(metadata.contains("\"protocolVersion\":\"1.0.0\""));
    assert!(metadata.contains("\"productionReady\":false"));
    Ok(())
}

#[test]
fn unavailable_product_work_is_not_advertised_as_success() -> Result<(), Box<dyn Error>> {
    let capabilities = invoke(["capabilities", "--output", "json"])?;
    assert!(capabilities.status.success());
    let capabilities = String::from_utf8(capabilities.stdout)?;
    assert!(
        capabilities.contains("\"id\":\"adapter-protocol\",\"owningIssue\":12,\"available\":true")
    );
    assert!(capabilities.contains("\"available\":false"));
    assert!(!capabilities.contains("\"productionReady\":true"));

    let unknown = invoke(["convert"])?;
    assert_eq!(unknown.status.code(), Some(2));
    assert!(String::from_utf8(unknown.stderr)?.contains("usage [FDIR-USAGE]"));
    Ok(())
}

#[test]
fn configuration_validation_is_deterministic_and_structured() -> Result<(), Box<dyn Error>> {
    let store = TempStore::new(91)?;
    let valid = store.write_fixture(
        std::path::Path::new("valid.conf"),
        b"output=json\nlog_level=debug\nredact_paths=true\ndeterministic_seed=91\n",
    )?;
    let valid_path = valid.to_string_lossy().into_owned();
    let result = invoke(["--config", &valid_path, "validate-config"])?;
    assert!(result.status.success());
    let stdout = String::from_utf8(result.stdout)?;
    assert!(stdout.contains("\"deterministicSeed\":91"));
    assert!(stdout.contains("\"productionReady\":false"));

    let invalid = store.write_fixture(std::path::Path::new("invalid.conf"), b"unknown=value\n")?;
    let invalid_path = invalid.to_string_lossy().into_owned();
    let result = invoke([
        "--output",
        "json",
        "--config",
        &invalid_path,
        "validate-config",
    ])?;
    assert_eq!(result.status.code(), Some(3));
    let stderr = String::from_utf8(result.stderr)?;
    assert!(stderr.contains("\"class\":\"validation\""));
    assert!(stderr.contains("FDIR-CONFIG-UNKNOWN-KEY"));
    Ok(())
}

#[test]
fn all_failure_classes_have_distinct_nonzero_codes() -> Result<(), Box<dyn Error>> {
    let result = invoke(["status-codes", "--output", "json"])?;
    assert!(result.status.success());
    let stdout = String::from_utf8(result.stdout)?;
    for expected in [
        "\"class\":\"usage\",\"exitCode\":2",
        "\"class\":\"validation\",\"exitCode\":3",
        "\"class\":\"unsupported\",\"exitCode\":4",
        "\"class\":\"partial\",\"exitCode\":5",
        "\"class\":\"policy\",\"exitCode\":6",
        "\"class\":\"resource-limit\",\"exitCode\":7",
        "\"class\":\"cancelled\",\"exitCode\":8",
        "\"class\":\"internal\",\"exitCode\":70",
    ] {
        assert!(stdout.contains(expected));
    }
    Ok(())
}
