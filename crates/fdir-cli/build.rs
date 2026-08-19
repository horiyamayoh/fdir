#![forbid(unsafe_code)]

use std::env;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=FDIR_SOURCE_REVISION");
    println!("cargo:rerun-if-changed=../../.git/HEAD");

    let revision = env::var("FDIR_SOURCE_REVISION")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(git_revision)
        .unwrap_or_else(|| "unknown".to_owned());
    let sanitized = sanitize(&revision);
    println!("cargo:rustc-env=FDIR_SOURCE_REVISION={sanitized}");
}

fn git_revision() -> Option<String> {
    let output = Command::new("git")
        .args(["rev-parse", "--short=12", "HEAD"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8(output.stdout)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn sanitize(value: &str) -> String {
    let result: String = value
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || "-_.".contains(*character))
        .take(64)
        .collect();
    if result.is_empty() {
        "unknown".to_owned()
    } else {
        result
    }
}
