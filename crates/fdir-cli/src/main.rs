#![forbid(unsafe_code)]

use std::env;
use std::process::ExitCode;

fn main() -> ExitCode {
    let arguments: Vec<String> = env::args().skip(1).collect();
    let execution = fdir_cli::run(&arguments);
    if !execution.stdout.is_empty() {
        let stdout = &execution.stdout;
        println!("{stdout}");
    }
    if !execution.stderr.is_empty() {
        let stderr = &execution.stderr;
        eprintln!("{stderr}");
    }
    ExitCode::from(execution.exit_code())
}
