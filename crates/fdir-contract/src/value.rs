#![forbid(unsafe_code)]

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt::{self, Display, Formatter};

/// Stable error returned when a foundational value cannot be constructed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValueError {
    code: &'static str,
    message: String,
}

impl ValueError {
    pub(crate) fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    /// Stable machine-readable error code.
    pub const fn code(&self) -> &'static str {
        self.code
    }

    /// Human-readable explanation.
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for ValueError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl Error for ValueError {}

/// Validated non-empty identifier text shared by generated strong-ID wrappers.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct Identifier(String);

impl Identifier {
    /// Construct an identifier without normalizing or rewriting its source spelling.
    pub fn new(value: impl Into<String>) -> Result<Self, ValueError> {
        let value = value.into();
        if value.is_empty() {
            return Err(ValueError::new(
                "FDIR-ID-EMPTY",
                "identifier text must not be empty",
            ));
        }
        if value.chars().any(char::is_control) {
            return Err(ValueError::new(
                "FDIR-ID-CONTROL",
                "identifier text must not contain control characters",
            ));
        }
        Ok(Self(value))
    }

    /// Borrow the exact identifier spelling.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Consume the identifier and return its exact spelling.
    pub fn into_string(self) -> String {
        self.0
    }
}

/// Content digest whose algorithm and exact hexadecimal value remain explicit.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct Digest(String);

impl Digest {
    /// Construct a current-baseline SHA-256 digest.
    pub fn new(value: impl Into<String>) -> Result<Self, ValueError> {
        let value = value.into();
        let Some(hex) = value.strip_prefix("sha256:") else {
            return Err(ValueError::new(
                "FDIR-DIGEST-ALGORITHM",
                "digest must use the sha256: prefix",
            ));
        };
        if hex.len() != 64
            || !hex
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        {
            return Err(ValueError::new(
                "FDIR-DIGEST-VALUE",
                "SHA-256 digest must contain exactly 64 lowercase hexadecimal digits",
            ));
        }
        Ok(Self(value))
    }

    /// Borrow the algorithm-qualified digest text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Display for Digest {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Finite numeric value used by confidence and geometry fields.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FiniteNumber(f64);

impl FiniteNumber {
    /// Reject NaN and infinity at the construction boundary.
    pub fn new(value: f64) -> Result<Self, ValueError> {
        if !value.is_finite() {
            return Err(ValueError::new(
                "FDIR-NUMBER-NON-FINITE",
                "FDIR values cannot contain NaN or infinity",
            ));
        }
        Ok(Self(value))
    }

    /// Return the finite numeric value.
    pub const fn get(self) -> f64 {
        self.0
    }
}

/// Exact JSON number spelling retained by the dependency-free parser.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct JsonNumber(String);

impl JsonNumber {
    fn new(raw: impl Into<String>) -> Result<Self, ValueError> {
        let raw = raw.into();
        if !is_valid_json_number(&raw) {
            return Err(ValueError::new(
                "FDIR-JSON-NUMBER",
                format!("invalid JSON number: {raw}"),
            ));
        }
        let parsed = raw.parse::<f64>().map_err(|error| {
            ValueError::new("FDIR-JSON-NUMBER", format!("invalid JSON number: {error}"))
        })?;
        if !parsed.is_finite() {
            return Err(ValueError::new(
                "FDIR-JSON-NUMBER",
                "JSON number is outside the finite range",
            ));
        }
        Ok(Self(raw))
    }

    /// Borrow the exact number spelling.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Parse the number as a finite `f64` for range validation.
    pub fn as_f64(&self) -> Option<f64> {
        self.0.parse::<f64>().ok()
    }

    /// Parse a non-negative integral value without accepting fractional syntax.
    pub fn as_u64(&self) -> Option<u64> {
        if self.0.contains('.')
            || self.0.contains('e')
            || self.0.contains('E')
            || self.0.starts_with('-')
        {
            return None;
        }
        self.0.parse::<u64>().ok()
    }
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

/// Format-neutral value used for assertion values, selectors, and extensions.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CanonicalValue {
    Null,
    Boolean(bool),
    Number(JsonNumber),
    String(String),
    Array(Vec<Self>),
    Object(ObjectValue),
}

/// Deterministically ordered object value.
pub type ObjectValue = BTreeMap<String, CanonicalValue>;

/// Unknown extension members retained independently from the generated core contract.
pub type ExtensionMap = BTreeMap<String, CanonicalValue>;

impl CanonicalValue {
    /// Parse JSON without a third-party parser or lossy number conversion.
    pub fn parse_json(input: &str) -> Result<Self, JsonError> {
        Parser::new(input).parse_document()
    }

    /// Serialize deterministically while preserving exact number spellings.
    pub fn to_json(&self) -> String {
        let mut output = String::new();
        self.write_json(&mut output);
        output
    }

    fn write_json(&self, output: &mut String) {
        match self {
            Self::Null => output.push_str("null"),
            Self::Boolean(value) => output.push_str(if *value { "true" } else { "false" }),
            Self::Number(value) => output.push_str(value.as_str()),
            Self::String(value) => write_json_string(value, output),
            Self::Array(values) => {
                output.push('[');
                for (index, value) in values.iter().enumerate() {
                    if index != 0 {
                        output.push(',');
                    }
                    value.write_json(output);
                }
                output.push(']');
            }
            Self::Object(values) => {
                output.push('{');
                for (index, (key, value)) in values.iter().enumerate() {
                    if index != 0 {
                        output.push(',');
                    }
                    write_json_string(key, output);
                    output.push(':');
                    value.write_json(output);
                }
                output.push('}');
            }
        }
    }

    /// Borrow an object value.
    pub const fn as_object(&self) -> Option<&ObjectValue> {
        if let Self::Object(value) = self {
            Some(value)
        } else {
            None
        }
    }

    /// Borrow an array value.
    pub fn as_array(&self) -> Option<&[Self]> {
        if let Self::Array(value) = self {
            Some(value)
        } else {
            None
        }
    }

    /// Borrow a string value.
    pub fn as_str(&self) -> Option<&str> {
        if let Self::String(value) = self {
            Some(value)
        } else {
            None
        }
    }

    /// Borrow a JSON number.
    pub const fn as_number(&self) -> Option<&JsonNumber> {
        if let Self::Number(value) = self {
            Some(value)
        } else {
            None
        }
    }

    /// Return a boolean value.
    pub const fn as_bool(&self) -> Option<bool> {
        if let Self::Boolean(value) = self {
            Some(*value)
        } else {
            None
        }
    }
}

fn write_json_string(value: &str, output: &mut String) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{0008}' => output.push_str("\\b"),
            '\u{000C}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value <= '\u{001F}' => {
                output.push_str(&format!("\\u{:04x}", u32::from(value)));
            }
            value => output.push(value),
        }
    }
    output.push('"');
}

/// Dependency-free JSON parse failure with byte position.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct JsonError {
    position: usize,
    message: String,
}

impl JsonError {
    fn new(position: usize, message: impl Into<String>) -> Self {
        Self {
            position,
            message: message.into(),
        }
    }

    /// Zero-based byte position where parsing failed.
    pub const fn position(&self) -> usize {
        self.position
    }

    /// Human-readable parse diagnostic.
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for JsonError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "JSON error at byte {}: {}",
            self.position, self.message
        )
    }
}

impl Error for JsonError {}

struct Parser<'a> {
    input: &'a str,
    index: usize,
}

impl<'a> Parser<'a> {
    const fn new(input: &'a str) -> Self {
        Self { input, index: 0 }
    }

    fn parse_document(mut self) -> Result<CanonicalValue, JsonError> {
        self.skip_whitespace();
        let value = self.parse_value()?;
        self.skip_whitespace();
        if self.index != self.input.len() {
            return Err(self.error("trailing content after JSON value"));
        }
        Ok(value)
    }

    fn parse_value(&mut self) -> Result<CanonicalValue, JsonError> {
        self.skip_whitespace();
        match self.peek_byte() {
            Some(b'n') => {
                self.consume_keyword("null")?;
                Ok(CanonicalValue::Null)
            }
            Some(b't') => {
                self.consume_keyword("true")?;
                Ok(CanonicalValue::Boolean(true))
            }
            Some(b'f') => {
                self.consume_keyword("false")?;
                Ok(CanonicalValue::Boolean(false))
            }
            Some(b'"') => self.parse_string().map(CanonicalValue::String),
            Some(b'[') => self.parse_array(),
            Some(b'{') => self.parse_object(),
            Some(b'-' | b'0'..=b'9') => self.parse_number(),
            Some(_) => Err(self.error("unexpected JSON token")),
            None => Err(self.error("unexpected end of JSON input")),
        }
    }

    fn parse_array(&mut self) -> Result<CanonicalValue, JsonError> {
        self.expect_byte(b'[')?;
        self.skip_whitespace();
        let mut values = Vec::new();
        if self.consume_if(b']') {
            return Ok(CanonicalValue::Array(values));
        }
        loop {
            values.push(self.parse_value()?);
            self.skip_whitespace();
            if self.consume_if(b']') {
                break;
            }
            self.expect_byte(b',')?;
            self.skip_whitespace();
        }
        Ok(CanonicalValue::Array(values))
    }

    fn parse_object(&mut self) -> Result<CanonicalValue, JsonError> {
        self.expect_byte(b'{')?;
        self.skip_whitespace();
        let mut values = BTreeMap::new();
        if self.consume_if(b'}') {
            return Ok(CanonicalValue::Object(values));
        }
        loop {
            if self.peek_byte() != Some(b'"') {
                return Err(self.error("object key must be a JSON string"));
            }
            let key = self.parse_string()?;
            self.skip_whitespace();
            self.expect_byte(b':')?;
            let value = self.parse_value()?;
            if values.insert(key.clone(), value).is_some() {
                return Err(self.error(format!("duplicate object key: {key}")));
            }
            self.skip_whitespace();
            if self.consume_if(b'}') {
                break;
            }
            self.expect_byte(b',')?;
            self.skip_whitespace();
        }
        Ok(CanonicalValue::Object(values))
    }

    fn parse_number(&mut self) -> Result<CanonicalValue, JsonError> {
        let start = self.index;
        while let Some(byte) = self.peek_byte() {
            if matches!(byte, b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E') {
                self.index += 1;
            } else {
                break;
            }
        }
        let raw = &self.input[start..self.index];
        JsonNumber::new(raw)
            .map(CanonicalValue::Number)
            .map_err(|error| JsonError::new(start, error.to_string()))
    }

    fn parse_string(&mut self) -> Result<String, JsonError> {
        self.expect_byte(b'"')?;
        let mut output = String::new();
        loop {
            let Some(byte) = self.peek_byte() else {
                return Err(self.error("unterminated JSON string"));
            };
            match byte {
                b'"' => {
                    self.index += 1;
                    return Ok(output);
                }
                b'\\' => {
                    self.index += 1;
                    self.parse_escape(&mut output)?;
                }
                0x00..=0x1f => return Err(self.error("unescaped control character in string")),
                _ => {
                    let Some(character) = self.input[self.index..].chars().next() else {
                        return Err(self.error("invalid UTF-8 string content"));
                    };
                    self.index += character.len_utf8();
                    output.push(character);
                }
            }
        }
    }

    fn parse_escape(&mut self, output: &mut String) -> Result<(), JsonError> {
        let Some(byte) = self.take_byte() else {
            return Err(self.error("unterminated JSON escape"));
        };
        match byte {
            b'"' => output.push('"'),
            b'\\' => output.push('\\'),
            b'/' => output.push('/'),
            b'b' => output.push('\u{0008}'),
            b'f' => output.push('\u{000C}'),
            b'n' => output.push('\n'),
            b'r' => output.push('\r'),
            b't' => output.push('\t'),
            b'u' => output.push(self.parse_unicode_escape()?),
            _ => return Err(self.error("unknown JSON escape")),
        }
        Ok(())
    }

    fn parse_unicode_escape(&mut self) -> Result<char, JsonError> {
        let first = self.parse_hex_quad()?;
        let scalar = if (0xd800..=0xdbff).contains(&first) {
            if self.take_byte() != Some(b'\\') || self.take_byte() != Some(b'u') {
                return Err(self.error("high surrogate must be followed by a low surrogate"));
            }
            let second = self.parse_hex_quad()?;
            if !(0xdc00..=0xdfff).contains(&second) {
                return Err(self.error("invalid low surrogate"));
            }
            0x1_0000 + ((u32::from(first) - 0xd800) << 10) + (u32::from(second) - 0xdc00)
        } else if (0xdc00..=0xdfff).contains(&first) {
            return Err(self.error("unexpected low surrogate"));
        } else {
            u32::from(first)
        };
        char::from_u32(scalar).ok_or_else(|| self.error("invalid Unicode scalar value"))
    }

    fn parse_hex_quad(&mut self) -> Result<u16, JsonError> {
        let mut value = 0_u16;
        for _ in 0..4 {
            let Some(byte) = self.take_byte() else {
                return Err(self.error("truncated Unicode escape"));
            };
            let Some(digit) = (byte as char).to_digit(16) else {
                return Err(self.error("invalid hexadecimal Unicode escape"));
            };
            value = value * 16 + digit as u16;
        }
        Ok(value)
    }

    fn consume_keyword(&mut self, keyword: &str) -> Result<(), JsonError> {
        if self.input[self.index..].starts_with(keyword) {
            self.index += keyword.len();
            Ok(())
        } else {
            Err(self.error(format!("expected {keyword}")))
        }
    }

    fn expect_byte(&mut self, expected: u8) -> Result<(), JsonError> {
        if self.consume_if(expected) {
            Ok(())
        } else {
            Err(self.error(format!("expected '{}'", char::from(expected))))
        }
    }

    fn consume_if(&mut self, expected: u8) -> bool {
        if self.peek_byte() == Some(expected) {
            self.index += 1;
            true
        } else {
            false
        }
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek_byte(), Some(b' ' | b'\n' | b'\r' | b'\t')) {
            self.index += 1;
        }
    }

    fn peek_byte(&self) -> Option<u8> {
        self.input.as_bytes().get(self.index).copied()
    }

    fn take_byte(&mut self) -> Option<u8> {
        let value = self.peek_byte()?;
        self.index += 1;
        Some(value)
    }

    fn error(&self, message: impl Into<String>) -> JsonError {
        JsonError::new(self.index, message)
    }
}

/// Unknown closed-enumeration value. It is never coerced to a known variant.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UnknownEnumValue {
    enum_name: &'static str,
    value: String,
}

impl UnknownEnumValue {
    pub(crate) fn new(enum_name: &'static str, value: impl Into<String>) -> Self {
        Self {
            enum_name,
            value: value.into(),
        }
    }

    /// Name of the closed logical-model enumeration.
    pub const fn enum_name(&self) -> &'static str {
        self.enum_name
    }

    /// Unrecognized source value.
    pub fn value(&self) -> &str {
        &self.value
    }
}

impl Display for UnknownEnumValue {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "unknown {} value: {}",
            self.enum_name, self.value
        )
    }
}

impl Error for UnknownEnumValue {}

#[cfg(test)]
mod tests {
    use super::{CanonicalValue, Digest, Identifier};

    #[test]
    fn parser_preserves_numbers_and_orders_object_keys() -> Result<(), Box<dyn std::error::Error>> {
        let value = CanonicalValue::parse_json(r#"{"z":1.25,"a":"é"}"#)?;
        assert_eq!(value.to_json(), r#"{"a":"é","z":1.25}"#);
        Ok(())
    }

    #[test]
    fn parser_rejects_duplicate_keys() {
        let result = CanonicalValue::parse_json(r#"{"a":1,"a":2}"#);
        assert!(result.is_err());
    }

    #[test]
    fn identifiers_and_digests_reject_invalid_values() {
        assert!(Identifier::new("").is_err());
        assert!(Digest::new("sha256:abc").is_err());
        assert!(Digest::new(format!("sha256:{}", "0".repeat(64))).is_ok());
    }
}
