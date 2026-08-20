#![forbid(unsafe_code)]

use std::error::Error;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

use fdir_adapter_sdk::{ProtocolLane, WireEnvelope, WireMessageKind};

fn repository_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn invoke(scenario: &str, request: Option<&str>) -> Result<Output, Box<dyn Error>> {
    let root = repository_root();
    let worker = root.join("tools/mock_adapter_worker.py");
    let temporary = std::env::temp_dir().join(format!(
        "fdir-non-rust-worker-{}-{scenario}",
        std::process::id()
    ));
    if temporary.exists() {
        fs::remove_dir_all(&temporary)?;
    }
    fs::create_dir_all(&temporary)?;
    let mut child = Command::new("python3")
        .arg(worker)
        .arg(scenario)
        .current_dir(&temporary)
        .env_clear()
        .env("FDIR_PROTOCOL_VERSION", "1.0.0")
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .stdin(if request.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    if let Some(request) = request {
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| io::Error::other("mock worker stdin is unavailable"))?;
        stdin.write_all(request.as_bytes())?;
        if !request.ends_with('\n') {
            stdin.write_all(b"\n")?;
        }
    }
    let output = child.wait_with_output()?;
    fs::remove_dir_all(&temporary)?;
    Ok(output)
}

fn envelopes(output: &Output) -> Result<Vec<WireEnvelope>, Box<dyn Error>> {
    let text = std::str::from_utf8(&output.stdout)?;
    text.lines()
        .filter(|line| !line.is_empty())
        .map(WireEnvelope::decode_json)
        .collect::<Result<Vec<_>, _>>()
        .map_err(Into::into)
}

#[test]
fn python_worker_uses_the_same_strict_protocol_and_is_deterministic() -> Result<(), Box<dyn Error>>
{
    let request = include_str!("../../../fixtures/adapter-protocol/valid-execute.json");
    let first = invoke("complete", Some(request))?;
    let second = invoke("complete", Some(request))?;
    assert!(first.status.success());
    assert_eq!(first.stdout, second.stdout);
    let decoded = envelopes(&first)?;
    assert_eq!(decoded.len(), 2);
    assert_eq!(decoded[0].kind, WireMessageKind::Output);
    assert_eq!(
        decoded[0].lane()?,
        Some(ProtocolLane::NativeSubstrateCensus)
    );
    assert_eq!(decoded[1].kind, WireMessageKind::Terminal);
    assert_eq!(
        decoded[1]
            .body
            .get("outcome")
            .and_then(fdir_core::CanonicalValue::as_str),
        Some("complete")
    );
    Ok(())
}

#[test]
fn python_worker_mismatch_and_crash_paths_do_not_become_success() -> Result<(), Box<dyn Error>> {
    let request = include_str!("../../../fixtures/adapter-protocol/valid-execute.json");
    let lane_mismatch = invoke("lane-mismatch", Some(request))?;
    assert!(lane_mismatch.status.success());
    let decoded = envelopes(&lane_mismatch)?;
    assert_eq!(decoded[0].lane()?, Some(ProtocolLane::SemanticHelper));

    let protocol_mismatch = invoke("protocol-mismatch", Some(request))?;
    assert!(protocol_mismatch.status.success());
    let first_line = std::str::from_utf8(&protocol_mismatch.stdout)?
        .lines()
        .next()
        .ok_or_else(|| io::Error::other("mock worker returned no protocol line"))?;
    assert_eq!(
        WireEnvelope::decode_json(first_line).map_err(|error| error.code()),
        Err("FDIR-PROTOCOL-VERSION-MISMATCH")
    );

    let crash = invoke("crash", Some(request))?;
    assert!(!crash.status.success());
    assert!(crash.stdout.is_empty());
    Ok(())
}

#[test]
fn python_worker_receives_no_ambient_credentials_or_repository_cwd() -> Result<(), Box<dyn Error>> {
    let output = invoke("environment", None)?;
    assert!(output.status.success());
    let text = std::str::from_utf8(&output.stdout)?;
    assert!(text.contains("\"homePresent\":false"));
    assert!(text.contains("\"credentialKeys\":[]"));
    assert!(
        !text.contains(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .to_string_lossy()
                .as_ref()
        )
    );
    Ok(())
}
