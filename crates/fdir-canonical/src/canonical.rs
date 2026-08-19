#![forbid(unsafe_code)]

use std::error::Error;
use std::fmt::{self, Display, Formatter};

use fdir_core::{CanonicalValue, Digest, JsonNumber};

use crate::sha256;

/// Version of the frozen FDIR 2.1 canonical JSON spelling rules.
pub const CANONICAL_JSON_VERSION: &str = "fdir-canonical-json/1";
/// Version of the domain-separated identity preimage contract.
pub const IDENTITY_VERSION: &str = "fdir-identity/1";

const MIN_INTEGER: i64 = i64::MIN;
const MAX_INTEGER: u64 = u64::MAX;

/// Durable canonicalization failure with a machine-readable code and JSON path.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CanonicalError {
    code: &'static str,
    path: String,
    message: String,
}

impl CanonicalError {
    pub(crate) fn new(
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

    /// JSON-style path to the rejected value.
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }

    /// Human-readable explanation without source-content expansion.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for CanonicalError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} at {}: {}",
            self.code, self.path, self.message
        )
    }
}

impl Error for CanonicalError {}

/// Serialize one value to the exact canonical UTF-8 bytes.
pub fn canonical_bytes(value: &CanonicalValue) -> Result<Vec<u8>, CanonicalError> {
    canonical_string(value).map(String::into_bytes)
}

/// Serialize one value to canonical JSON text.
pub fn canonical_string(value: &CanonicalValue) -> Result<String, CanonicalError> {
    let mut output = String::new();
    write_value(value, "$", &mut output)?;
    Ok(output)
}

/// Strictly parse JSON and return its canonical UTF-8 bytes.
pub fn canonicalize_json(input: &str) -> Result<Vec<u8>, CanonicalError> {
    if matches!(input.trim(), "NaN" | "Infinity" | "-Infinity") {
        return Err(CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-NON-FINITE",
            "$",
            "canonical JSON forbids NaN and infinity",
        ));
    }
    let value = CanonicalValue::parse_json(input).map_err(|error| {
        let message = error.message();
        let code = if message.contains("duplicate object key") {
            "FDIR-CANONICAL-DUPLICATE-KEY"
        } else if message.contains("outside the finite range") {
            "FDIR-CANONICAL-NUMBER-NON-FINITE"
        } else {
            "FDIR-CANONICAL-JSON-SYNTAX"
        };
        CanonicalError::new(code, "$", error.to_string())
    })?;
    canonical_bytes(&value)
}

/// Return whether the input is already the exact canonical JSON spelling.
pub fn is_canonical_json(input: &str) -> Result<bool, CanonicalError> {
    Ok(canonicalize_json(input)? == input.as_bytes())
}

/// Compute the frozen plain SHA-256 content digest over canonical bytes.
pub fn content_digest(value: &CanonicalValue) -> Result<Digest, CanonicalError> {
    let bytes = canonical_bytes(value)?;
    digest_bytes(&bytes)
}

/// Compute an identity digest over a canonical envelope and an explicit domain.
pub fn domain_separated_digest(
    kind: &str,
    value: &CanonicalValue,
) -> Result<Digest, CanonicalError> {
    if kind.is_empty() || kind.contains('\0') || !kind.is_ascii() {
        return Err(CanonicalError::new(
            "FDIR-IDENTITY-DOMAIN",
            "$",
            "identity kind must be non-empty ASCII without NUL",
        ));
    }
    let canonical = canonical_bytes(value)?;
    let mut preimage =
        Vec::with_capacity(8 + IDENTITY_VERSION.len() + kind.len() + canonical.len());
    preimage.extend_from_slice(b"FDIR-ID\0");
    preimage.extend_from_slice(IDENTITY_VERSION.as_bytes());
    preimage.push(0);
    preimage.extend_from_slice(kind.as_bytes());
    preimage.push(0);
    preimage.extend_from_slice(&canonical);
    digest_bytes(&preimage)
}

pub(crate) fn digest_bytes(bytes: &[u8]) -> Result<Digest, CanonicalError> {
    let value = format!("sha256:{}", sha256::hexadecimal(bytes));
    Digest::new(value).map_err(|error| {
        CanonicalError::new(
            "FDIR-CANONICAL-DIGEST",
            "$",
            format!("constructed digest was invalid: {error}"),
        )
    })
}

fn write_value(
    value: &CanonicalValue,
    path: &str,
    output: &mut String,
) -> Result<(), CanonicalError> {
    match value {
        CanonicalValue::Null => output.push_str("null"),
        CanonicalValue::Boolean(value) => {
            output.push_str(if *value { "true" } else { "false" });
        }
        CanonicalValue::Number(value) => output.push_str(&canonical_number(value, path)?),
        CanonicalValue::String(value) => write_json_string(value, output),
        CanonicalValue::Array(values) => {
            output.push('[');
            for (index, item) in values.iter().enumerate() {
                if index != 0 {
                    output.push(',');
                }
                let item_path = format!("{path}/{index}");
                write_value(item, &item_path, output)?;
            }
            output.push(']');
        }
        CanonicalValue::Object(values) => {
            output.push('{');
            for (index, (key, item)) in values.iter().enumerate() {
                if index != 0 {
                    output.push(',');
                }
                write_json_string(key, output);
                output.push(':');
                let item_path = format!("{path}/{}", pointer_token(key));
                write_value(item, &item_path, output)?;
            }
            output.push('}');
        }
    }
    Ok(())
}

fn write_json_string(value: &str, output: &mut String) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{0008}' => output.push_str("\\b"),
            '\u{000c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            control if control <= '\u{001f}' => {
                output.push_str(&format!("\\u{:04x}", u32::from(control)));
            }
            other => output.push(other),
        }
    }
    output.push('"');
}

fn canonical_number(value: &JsonNumber, path: &str) -> Result<String, CanonicalError> {
    canonical_number_spelling(value.as_str(), path)
}

fn canonical_number_spelling(raw: &str, path: &str) -> Result<String, CanonicalError> {
    if !is_valid_json_number(raw) {
        return Err(CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-SYNTAX",
            path,
            "number is not valid JSON numeric syntax",
        ));
    }
    if !raw.bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
        if raw == "-0" {
            return Ok("0".to_owned());
        }
        let in_range = if raw.starts_with('-') {
            raw.parse::<i64>().is_ok()
        } else {
            raw.parse::<u64>().is_ok()
        };
        if !in_range {
            return Err(CanonicalError::new(
                "FDIR-CANONICAL-INTEGER-RANGE",
                path,
                format!("integer must be between {MIN_INTEGER} and {MAX_INTEGER}"),
            ));
        }
        return Ok(raw.to_owned());
    }

    let number = raw.parse::<f64>().map_err(|error| {
        CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-SYNTAX",
            path,
            format!("number cannot be represented as binary64: {error}"),
        )
    })?;
    if !number.is_finite() {
        return Err(CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-NON-FINITE",
            path,
            "JSON number is outside the finite binary64 range",
        ));
    }
    if number == 0.0 && raw.bytes().any(|byte| matches!(byte, b'1'..=b'9')) {
        return Err(CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-UNDERFLOW",
            path,
            "JSON number underflows finite binary64",
        ));
    }
    format_python_float(number, path)
}

fn format_python_float(value: f64, path: &str) -> Result<String, CanonicalError> {
    if value == 0.0 {
        return Ok(if value.is_sign_negative() {
            "-0.0".to_owned()
        } else {
            "0.0".to_owned()
        });
    }

    let negative = value.is_sign_negative();
    let source = value.abs().to_string();
    let (mantissa, explicit_exponent) = if let Some(index) = source.find(['e', 'E']) {
        let exponent = source[index + 1..].parse::<i32>().map_err(|error| {
            CanonicalError::new(
                "FDIR-CANONICAL-NUMBER-INTERNAL",
                path,
                format!("Rust float exponent could not be parsed: {error}"),
            )
        })?;
        (&source[..index], exponent)
    } else {
        (source.as_str(), 0)
    };
    let decimal_index = mantissa.find('.').unwrap_or(mantissa.len());
    let digits: String = mantissa
        .chars()
        .filter(|character| *character != '.')
        .collect();
    let leading = digits
        .bytes()
        .position(|byte| byte != b'0')
        .ok_or_else(|| {
            CanonicalError::new(
                "FDIR-CANONICAL-NUMBER-INTERNAL",
                path,
                "non-zero float lost all significant digits",
            )
        })?;
    let mut significant = digits[leading..].to_owned();
    while significant.ends_with('0') {
        significant.pop();
    }
    if significant.is_empty() {
        return Err(CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-INTERNAL",
            path,
            "non-zero float has no significant digits",
        ));
    }
    let decimal_index = i32::try_from(decimal_index).map_err(|error| {
        CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-INTERNAL",
            path,
            format!("float decimal index is unsupported: {error}"),
        )
    })?;
    let leading = i32::try_from(leading).map_err(|error| {
        CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-INTERNAL",
            path,
            format!("float leading-zero count is unsupported: {error}"),
        )
    })?;
    let decimal_position = decimal_index + explicit_exponent - leading;
    let scientific_exponent = decimal_position - 1;

    let mut output = String::new();
    if negative {
        output.push('-');
    }
    if (-4..16).contains(&scientific_exponent) {
        write_fixed_float(&significant, decimal_position, path, &mut output)?;
    } else {
        write_scientific_float(&significant, scientific_exponent, path, &mut output)?;
    }
    Ok(output)
}

fn write_fixed_float(
    significant: &str,
    decimal_position: i32,
    path: &str,
    output: &mut String,
) -> Result<(), CanonicalError> {
    if decimal_position <= 0 {
        output.push_str("0.");
        let zero_count = decimal_position
            .checked_neg()
            .and_then(|value| usize::try_from(value).ok())
            .ok_or_else(|| {
                CanonicalError::new(
                    "FDIR-CANONICAL-NUMBER-INTERNAL",
                    path,
                    "fixed float zero count is unsupported",
                )
            })?;
        for _ in 0..zero_count {
            output.push('0');
        }
        output.push_str(significant);
        return Ok(());
    }

    let position = usize::try_from(decimal_position).map_err(|error| {
        CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-INTERNAL",
            path,
            format!("fixed float decimal position is unsupported: {error}"),
        )
    })?;
    if position >= significant.len() {
        output.push_str(significant);
        for _ in significant.len()..position {
            output.push('0');
        }
        output.push_str(".0");
    } else {
        let (integer, fraction) = significant.split_at(position);
        output.push_str(integer);
        output.push('.');
        output.push_str(fraction);
    }
    Ok(())
}

fn write_scientific_float(
    significant: &str,
    exponent: i32,
    path: &str,
    output: &mut String,
) -> Result<(), CanonicalError> {
    let mut characters = significant.chars();
    let first = characters.next().ok_or_else(|| {
        CanonicalError::new(
            "FDIR-CANONICAL-NUMBER-INTERNAL",
            path,
            "scientific float has no significant digit",
        )
    })?;
    output.push(first);
    let remaining = characters.as_str();
    if !remaining.is_empty() {
        output.push('.');
        output.push_str(remaining);
    }
    let sign = if exponent < 0 { '-' } else { '+' };
    let absolute = exponent.unsigned_abs();
    output.push_str(&format!("e{sign}{absolute:02}"));
    Ok(())
}

fn pointer_token(value: &str) -> String {
    value.replace('~', "~0").replace('/', "~1")
}

fn is_valid_json_number(raw: &str) -> bool {
    let bytes = raw.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let mut index = usize::from(bytes[0] == b'-');
    if index == bytes.len() {
        return false;
    }
    match bytes[index] {
        b'0' => {
            index += 1;
            if index < bytes.len() && bytes[index].is_ascii_digit() {
                return false;
            }
        }
        b'1'..=b'9' => {
            index += 1;
            while index < bytes.len() && bytes[index].is_ascii_digit() {
                index += 1;
            }
        }
        _ => return false,
    }
    if index < bytes.len() && bytes[index] == b'.' {
        index += 1;
        let start = index;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        if index == start {
            return false;
        }
    }
    if index < bytes.len() && matches!(bytes[index], b'e' | b'E') {
        index += 1;
        if index < bytes.len() && matches!(bytes[index], b'+' | b'-') {
            index += 1;
        }
        let start = index;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        if index == start {
            return false;
        }
    }
    index == bytes.len()
}

#[cfg(test)]
mod tests {
    use super::{canonicalize_json, content_digest, domain_separated_digest, is_canonical_json};
    use fdir_core::CanonicalValue;

    fn parse(value: &str) -> Result<CanonicalValue, Box<dyn std::error::Error>> {
        Ok(CanonicalValue::parse_json(value)?)
    }

    #[test]
    fn nested_vector_matches_the_python_authority() -> Result<(), Box<dyn std::error::Error>> {
        let input = r#"{"z":[3,{"é":"value","a":true}],"a":{"number":1.25,"n":null}}"#;
        let canonical = canonicalize_json(input)?;
        assert_eq!(
            String::from_utf8(canonical)?,
            r#"{"a":{"n":null,"number":1.25},"z":[3,{"a":true,"é":"value"}]}"#
        );
        let digest = content_digest(&parse(input)?)?;
        assert_eq!(
            digest.as_str(),
            "sha256:2d464b2fdfdead3b231fa814e2e744856b9281de7ca82199c27d46b3b78d2fa1"
        );
        Ok(())
    }

    #[test]
    fn numeric_spelling_matches_python_boundaries() -> Result<(), Box<dyn std::error::Error>> {
        let cases = [
            ("-0", "0"),
            ("-0.0", "-0.0"),
            ("0e0", "0.0"),
            ("1e0", "1.0"),
            ("1e-4", "0.0001"),
            ("1e-5", "1e-05"),
            ("1e15", "1000000000000000.0"),
            ("1e16", "1e+16"),
            ("1.2500E+03", "1250.0"),
            ("5e-324", "5e-324"),
        ];
        for (input, expected) in cases {
            assert_eq!(String::from_utf8(canonicalize_json(input)?)?, expected);
        }
        Ok(())
    }

    #[test]
    fn explicit_rejections_keep_durable_codes() {
        let cases = [
            ("18446744073709551616", "FDIR-CANONICAL-INTEGER-RANGE"),
            ("1e400", "FDIR-CANONICAL-NUMBER-NON-FINITE"),
            ("1e-400", "FDIR-CANONICAL-NUMBER-UNDERFLOW"),
            (r#"{"a":1,"a":2}"#, "FDIR-CANONICAL-DUPLICATE-KEY"),
        ];
        for (input, expected) in cases {
            let result = canonicalize_json(input);
            assert_eq!(
                result.as_ref().err().map(super::CanonicalError::code),
                Some(expected)
            );
        }
    }

    #[test]
    fn domain_separation_changes_identity_digest() -> Result<(), Box<dyn std::error::Error>> {
        let value = parse(r#"{"value":"same"}"#)?;
        let artifact = domain_separated_digest("artifact", &value)?;
        let selector = domain_separated_digest("selector", &value)?;
        assert_ne!(artifact, selector);
        assert!(is_canonical_json(r#"{"value":"same"}"#)?);
        assert!(!is_canonical_json(r#"{ "value": "same" }"#)?);
        Ok(())
    }
}
