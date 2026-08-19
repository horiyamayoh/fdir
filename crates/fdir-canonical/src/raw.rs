#![forbid(unsafe_code)]

use fdir_core::Digest;

use crate::CanonicalError;

/// Compute a plain SHA-256 digest over exact, already-defined bytes.
///
/// This is the content-addressing primitive for non-JSON evidence and canonical snapshot bytes. It
/// does not add an identity domain separator and therefore must not be used in place of entity
/// identity construction.
pub fn raw_content_digest(bytes: &[u8]) -> Result<Digest, CanonicalError> {
    let value = format!("sha256:{}", crate::sha256::hexadecimal(bytes));
    Digest::new(value).map_err(|error| {
        CanonicalError::new(
            "FDIR-CANONICAL-DIGEST",
            "$",
            format!("constructed digest was invalid: {error}"),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::raw_content_digest;

    #[test]
    fn hashes_exact_bytes_without_json_normalization() {
        let digest = raw_content_digest(b"abc");
        assert_eq!(
            digest.as_ref().map(|value| value.as_str()),
            Ok("sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        );
    }
}
