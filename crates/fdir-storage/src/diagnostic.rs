#![forbid(unsafe_code)]

use std::error::Error;
use std::fmt::{self, Display, Formatter};

/// One stable structural, semantic, or storage-integrity diagnostic.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StorageDiagnostic {
    code: &'static str,
    path: String,
    message: String,
}

impl StorageDiagnostic {
    /// Construct a diagnostic with a machine-readable code and JSON-style path.
    #[must_use]
    pub fn new(
        code: &'static str,
        path: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            code,
            path: path.into(),
            message: message.into(),
        }
    }

    /// Stable machine-readable diagnostic code.
    #[must_use]
    pub const fn code(&self) -> &'static str {
        self.code
    }

    /// JSON-style path or storage-relative location associated with the diagnostic.
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }

    /// Human-readable explanation that never expands source or evidence bytes.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for StorageDiagnostic {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} at {}: {}",
            self.code, self.path, self.message
        )
    }
}

/// Durable storage failure carrying one stable diagnostic.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StorageError {
    diagnostic: StorageDiagnostic,
}

impl StorageError {
    /// Construct a storage failure.
    #[must_use]
    pub fn new(
        code: &'static str,
        path: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            diagnostic: StorageDiagnostic::new(code, path, message),
        }
    }

    /// Construct a storage failure from an existing diagnostic.
    #[must_use]
    pub fn from_diagnostic(diagnostic: StorageDiagnostic) -> Self {
        Self { diagnostic }
    }

    /// Stable machine-readable failure code.
    #[must_use]
    pub const fn code(&self) -> &'static str {
        self.diagnostic.code()
    }

    /// JSON-style path or storage-relative location associated with the failure.
    #[must_use]
    pub fn path(&self) -> &str {
        self.diagnostic.path()
    }

    /// Human-readable failure explanation.
    #[must_use]
    pub fn message(&self) -> &str {
        self.diagnostic.message()
    }

    /// Borrow the complete diagnostic.
    #[must_use]
    pub const fn diagnostic(&self) -> &StorageDiagnostic {
        &self.diagnostic
    }
}

impl Display for StorageError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        Display::fmt(&self.diagnostic, formatter)
    }
}

impl Error for StorageError {}

/// Deterministically ordered validation diagnostics.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ValidationReport {
    diagnostics: Vec<StorageDiagnostic>,
}

impl ValidationReport {
    /// Construct an empty, valid report.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            diagnostics: Vec::new(),
        }
    }

    /// Whether no validation defect was found.
    #[must_use]
    pub fn is_valid(&self) -> bool {
        self.diagnostics.is_empty()
    }

    /// Number of diagnostics in the report.
    #[must_use]
    pub fn len(&self) -> usize {
        self.diagnostics.len()
    }

    /// Whether the report contains no diagnostics.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.diagnostics.is_empty()
    }

    /// Borrow diagnostics in deterministic order.
    #[must_use]
    pub fn diagnostics(&self) -> &[StorageDiagnostic] {
        &self.diagnostics
    }

    pub(crate) fn push(&mut self, diagnostic: StorageDiagnostic) {
        self.diagnostics.push(diagnostic);
    }

    pub(crate) fn into_result(mut self) -> Result<(), StorageError> {
        self.diagnostics.sort_by(|left, right| {
            (left.path(), left.code(), left.message()).cmp(&(
                right.path(),
                right.code(),
                right.message(),
            ))
        });
        match self.diagnostics.into_iter().next() {
            Some(diagnostic) => Err(StorageError::from_diagnostic(diagnostic)),
            None => Ok(()),
        }
    }
}
