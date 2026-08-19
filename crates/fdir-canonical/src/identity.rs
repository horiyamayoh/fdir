#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt::{self, Display, Formatter};

use fdir_core::{CanonicalValue, Digest, ObjectValue};

use crate::{CanonicalError, domain_separated_digest};

/// Canonical envelope schema included in every identity preimage.
pub const IDENTITY_MATERIAL_SCHEMA: &str = "fdir/identity-material/1";

/// Identity domains in the frozen acyclic construction order.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum IdentityKind {
    Artifact,
    Selector,
    InformationUnit,
    Carrier,
    ReferencedObject,
    Occurrence,
    Evidence,
    RecordAssertion,
    Snapshot,
}

impl IdentityKind {
    /// Stable domain name used in canonical envelopes and digest preimages.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Artifact => "artifact",
            Self::Selector => "selector",
            Self::InformationUnit => "information-unit",
            Self::Carrier => "carrier",
            Self::ReferencedObject => "referenced-object",
            Self::Occurrence => "occurrence",
            Self::Evidence => "evidence",
            Self::RecordAssertion => "record-assertion",
            Self::Snapshot => "snapshot",
        }
    }

    const fn rank(self) -> u8 {
        match self {
            Self::Artifact | Self::Selector | Self::InformationUnit => 0,
            Self::Carrier | Self::ReferencedObject => 1,
            Self::Occurrence => 2,
            Self::Evidence => 3,
            Self::RecordAssertion => 4,
            Self::Snapshot => 5,
        }
    }
}

/// One named edge to an earlier identity node.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentityReference {
    role: String,
    target: String,
}

impl IdentityReference {
    /// Construct a reference without putting repository-local keys into identity material.
    pub fn new(role: impl Into<String>, target: impl Into<String>) -> Result<Self, IdentityError> {
        let role = role.into();
        let target = target.into();
        validate_token("reference role", &role)?;
        validate_token("reference target", &target)?;
        Ok(Self { role, target })
    }

    /// Stable semantic role of this edge.
    #[must_use]
    pub fn role(&self) -> &str {
        &self.role
    }

    /// Repository-local target key used only to resolve the DAG.
    #[must_use]
    pub fn target(&self) -> &str {
        &self.target
    }
}

/// Stable identity material plus references and explicitly excluded operational metadata.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentityNode {
    key: String,
    kind: IdentityKind,
    material: CanonicalValue,
    references: Vec<IdentityReference>,
    operational_metadata: Option<CanonicalValue>,
}

impl IdentityNode {
    /// Construct a node whose local key is not part of its identity preimage.
    pub fn new(
        key: impl Into<String>,
        kind: IdentityKind,
        material: CanonicalValue,
        references: Vec<IdentityReference>,
    ) -> Result<Self, IdentityError> {
        let key = key.into();
        validate_token("identity node key", &key)?;
        Ok(Self {
            key,
            kind,
            material,
            references,
            operational_metadata: None,
        })
    }

    /// Attach mutable timing, projection, index, or transport metadata excluded from identity.
    #[must_use]
    pub fn with_operational_metadata(mut self, metadata: CanonicalValue) -> Self {
        self.operational_metadata = Some(metadata);
        self
    }

    /// Repository-local node key.
    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }

    /// Identity domain.
    #[must_use]
    pub const fn kind(&self) -> IdentityKind {
        self.kind
    }

    /// Stable material included in identity.
    #[must_use]
    pub const fn material(&self) -> &CanonicalValue {
        &self.material
    }

    /// References included by role and target digest, never by local target key.
    #[must_use]
    pub fn references(&self) -> &[IdentityReference] {
        &self.references
    }

    /// Operational metadata retained by the caller but excluded from identity.
    #[must_use]
    pub const fn operational_metadata(&self) -> Option<&CanonicalValue> {
        self.operational_metadata.as_ref()
    }
}

/// Validated set of nodes from which deterministic identities can be computed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentityDag {
    nodes: BTreeMap<String, IdentityNode>,
}

impl IdentityDag {
    /// Validate local keys, target existence, and duplicate edge declarations.
    pub fn new(nodes: Vec<IdentityNode>) -> Result<Self, IdentityError> {
        if nodes.is_empty() {
            return Err(IdentityError::new(
                "FDIR-IDENTITY-DAG-EMPTY",
                None,
                "identity DAG must contain at least one node",
            ));
        }
        let mut by_key = BTreeMap::new();
        for node in nodes {
            let key = node.key.clone();
            if by_key.insert(key.clone(), node).is_some() {
                return Err(IdentityError::new(
                    "FDIR-IDENTITY-DUPLICATE-NODE",
                    Some(key),
                    "identity node key is declared more than once",
                ));
            }
        }
        for node in by_key.values() {
            let mut edges = BTreeSet::new();
            for reference in &node.references {
                if !by_key.contains_key(&reference.target) {
                    return Err(IdentityError::new(
                        "FDIR-IDENTITY-MISSING-TARGET",
                        Some(node.key.clone()),
                        format!("reference target does not exist: {}", reference.target),
                    ));
                }
                if !edges.insert((reference.role.clone(), reference.target.clone())) {
                    return Err(IdentityError::new(
                        "FDIR-IDENTITY-DUPLICATE-REFERENCE",
                        Some(node.key.clone()),
                        format!(
                            "duplicate identity reference: {} -> {}",
                            reference.role, reference.target
                        ),
                    ));
                }
            }
        }
        Ok(Self { nodes: by_key })
    }

    /// Compute every digest in dependency order and reject cycles or forward identity edges.
    pub fn compute(&self) -> Result<IdentityResult, IdentityError> {
        let order = self.topological_order()?;
        self.validate_kind_order()?;
        let mut digests = BTreeMap::new();
        for key in &order {
            let node = self.nodes.get(key).ok_or_else(|| {
                IdentityError::new(
                    "FDIR-IDENTITY-MISSING-NODE",
                    Some(key.clone()),
                    "topological order references an unknown node",
                )
            })?;
            let envelope = identity_envelope(node, &digests, &self.nodes)?;
            let digest = domain_separated_digest(node.kind.as_str(), &envelope)
                .map_err(|error| identity_canonical_error(node, &error))?;
            digests.insert(
                key.clone(),
                IdentityDigest {
                    kind: node.kind,
                    digest,
                },
            );
        }
        Ok(IdentityResult { digests, order })
    }

    fn topological_order(&self) -> Result<Vec<String>, IdentityError> {
        let mut states = BTreeMap::new();
        let mut stack = Vec::new();
        let mut order = Vec::with_capacity(self.nodes.len());
        for key in self.nodes.keys() {
            visit(key, &self.nodes, &mut states, &mut stack, &mut order)?;
        }
        Ok(order)
    }

    fn validate_kind_order(&self) -> Result<(), IdentityError> {
        for node in self.nodes.values() {
            for reference in &node.references {
                let target = self.nodes.get(&reference.target).ok_or_else(|| {
                    IdentityError::new(
                        "FDIR-IDENTITY-MISSING-TARGET",
                        Some(node.key.clone()),
                        format!("reference target does not exist: {}", reference.target),
                    )
                })?;
                if node.kind.rank() <= target.kind.rank() {
                    return Err(IdentityError::new(
                        "FDIR-IDENTITY-DAG-ORDER",
                        Some(node.key.clone()),
                        format!(
                            "{} identity cannot depend on {} identity",
                            node.kind.as_str(),
                            target.kind.as_str()
                        ),
                    ));
                }
            }
        }
        Ok(())
    }
}

/// One domain-qualified identity digest.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentityDigest {
    kind: IdentityKind,
    digest: Digest,
}

impl IdentityDigest {
    /// Identity domain used for this digest.
    #[must_use]
    pub const fn kind(&self) -> IdentityKind {
        self.kind
    }

    /// Algorithm-qualified digest.
    #[must_use]
    pub const fn digest(&self) -> &Digest {
        &self.digest
    }

    /// Borrow the `sha256:` spelling.
    #[must_use]
    pub fn as_str(&self) -> &str {
        self.digest.as_str()
    }
}

/// Deterministic DAG result in dependency-first order.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentityResult {
    digests: BTreeMap<String, IdentityDigest>,
    order: Vec<String>,
}

impl IdentityResult {
    /// Resolve one local node key to its identity digest.
    #[must_use]
    pub fn digest(&self, key: &str) -> Option<&IdentityDigest> {
        self.digests.get(key)
    }

    /// Dependency-first local computation order.
    #[must_use]
    pub fn order(&self) -> &[String] {
        &self.order
    }

    /// Iterate local keys and digests in deterministic key order.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &IdentityDigest)> {
        self.digests
            .iter()
            .map(|(key, digest)| (key.as_str(), digest))
    }
}

/// Durable identity-construction failure.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentityError {
    code: &'static str,
    node: Option<String>,
    message: String,
}

impl IdentityError {
    fn new(code: &'static str, node: Option<String>, message: impl Into<String>) -> Self {
        Self {
            code,
            node,
            message: message.into(),
        }
    }

    /// Stable machine-readable diagnostic code.
    #[must_use]
    pub const fn code(&self) -> &'static str {
        self.code
    }

    /// Local node key associated with the failure, when available.
    #[must_use]
    pub fn node(&self) -> Option<&str> {
        self.node.as_deref()
    }

    /// Human-readable explanation.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for IdentityError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        if let Some(node) = &self.node {
            write!(formatter, "{} at {}: {}", self.code, node, self.message)
        } else {
            write!(formatter, "{}: {}", self.code, self.message)
        }
    }
}

impl Error for IdentityError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum VisitState {
    Visiting,
    Complete,
}

fn visit(
    key: &str,
    nodes: &BTreeMap<String, IdentityNode>,
    states: &mut BTreeMap<String, VisitState>,
    stack: &mut Vec<String>,
    order: &mut Vec<String>,
) -> Result<(), IdentityError> {
    match states.get(key).copied() {
        Some(VisitState::Complete) => return Ok(()),
        Some(VisitState::Visiting) => {
            let start = stack.iter().position(|item| item == key).unwrap_or(0);
            let mut cycle = stack[start..].to_vec();
            cycle.push(key.to_owned());
            return Err(IdentityError::new(
                "FDIR-IDENTITY-DAG-CYCLE",
                Some(key.to_owned()),
                format!("identity cycle: {}", cycle.join(" -> ")),
            ));
        }
        None => {}
    }

    states.insert(key.to_owned(), VisitState::Visiting);
    stack.push(key.to_owned());
    let node = nodes.get(key).ok_or_else(|| {
        IdentityError::new(
            "FDIR-IDENTITY-MISSING-NODE",
            Some(key.to_owned()),
            "identity traversal reached an unknown node",
        )
    })?;
    let mut targets: Vec<&str> = node
        .references
        .iter()
        .map(|reference| reference.target.as_str())
        .collect();
    targets.sort_unstable();
    targets.dedup();
    for target in targets {
        visit(target, nodes, states, stack, order)?;
    }
    let popped = stack.pop();
    if popped.as_deref() != Some(key) {
        return Err(IdentityError::new(
            "FDIR-IDENTITY-DAG-STACK",
            Some(key.to_owned()),
            "identity traversal stack became inconsistent",
        ));
    }
    states.insert(key.to_owned(), VisitState::Complete);
    order.push(key.to_owned());
    Ok(())
}

fn identity_envelope(
    node: &IdentityNode,
    digests: &BTreeMap<String, IdentityDigest>,
    nodes: &BTreeMap<String, IdentityNode>,
) -> Result<CanonicalValue, IdentityError> {
    let mut resolved = Vec::with_capacity(node.references.len());
    for reference in &node.references {
        let target_digest = digests.get(&reference.target).ok_or_else(|| {
            IdentityError::new(
                "FDIR-IDENTITY-UNRESOLVED-TARGET",
                Some(node.key.clone()),
                format!("target digest is not available: {}", reference.target),
            )
        })?;
        let target_node = nodes.get(&reference.target).ok_or_else(|| {
            IdentityError::new(
                "FDIR-IDENTITY-MISSING-TARGET",
                Some(node.key.clone()),
                format!("reference target does not exist: {}", reference.target),
            )
        })?;
        resolved.push((
            reference.role.clone(),
            target_node.kind.as_str().to_owned(),
            target_digest.as_str().to_owned(),
        ));
    }
    resolved.sort();

    let references = resolved
        .into_iter()
        .map(|(role, target_kind, target_digest)| {
            let mut value = ObjectValue::new();
            value.insert("role".to_owned(), CanonicalValue::String(role));
            value.insert(
                "targetDigest".to_owned(),
                CanonicalValue::String(target_digest),
            );
            value.insert("targetKind".to_owned(), CanonicalValue::String(target_kind));
            CanonicalValue::Object(value)
        })
        .collect();

    let mut envelope = ObjectValue::new();
    envelope.insert(
        "kind".to_owned(),
        CanonicalValue::String(node.kind.as_str().to_owned()),
    );
    envelope.insert("material".to_owned(), node.material.clone());
    envelope.insert("references".to_owned(), CanonicalValue::Array(references));
    envelope.insert(
        "schema".to_owned(),
        CanonicalValue::String(IDENTITY_MATERIAL_SCHEMA.to_owned()),
    );
    Ok(CanonicalValue::Object(envelope))
}

fn identity_canonical_error(node: &IdentityNode, error: &CanonicalError) -> IdentityError {
    IdentityError::new(
        "FDIR-IDENTITY-CANONICAL",
        Some(node.key.clone()),
        format!("{}: {}", error.code(), error.message()),
    )
}

fn validate_token(label: &str, value: &str) -> Result<(), IdentityError> {
    if value.is_empty() {
        return Err(IdentityError::new(
            "FDIR-IDENTITY-TOKEN-EMPTY",
            None,
            format!("{label} must not be empty"),
        ));
    }
    if value.chars().any(char::is_control) {
        return Err(IdentityError::new(
            "FDIR-IDENTITY-TOKEN-CONTROL",
            None,
            format!("{label} must not contain control characters"),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{IdentityDag, IdentityKind, IdentityNode, IdentityReference};
    use fdir_core::CanonicalValue;

    fn value(input: &str) -> Result<CanonicalValue, Box<dyn std::error::Error>> {
        Ok(CanonicalValue::parse_json(input)?)
    }

    fn reference(
        role: &str,
        target: &str,
    ) -> Result<IdentityReference, Box<dyn std::error::Error>> {
        Ok(IdentityReference::new(role, target)?)
    }

    fn example_dag(
        assertion_value: &str,
        metadata: &str,
    ) -> Result<IdentityDag, Box<dyn std::error::Error>> {
        let artifact = IdentityNode::new(
            "artifact-local",
            IdentityKind::Artifact,
            value(
                r#"{"digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
            )?,
            Vec::new(),
        )?;
        let unit = IdentityNode::new(
            "unit-local",
            IdentityKind::InformationUnit,
            value(r#"{"anchor":"paragraph"}"#)?,
            Vec::new(),
        )?;
        let evidence = IdentityNode::new(
            "evidence-local",
            IdentityKind::Evidence,
            value(r#"{"selector":"bytes:0-4"}"#)?,
            vec![reference("artifact", "artifact-local")?],
        )?;
        let assertion = IdentityNode::new(
            "assertion-local",
            IdentityKind::RecordAssertion,
            value(assertion_value)?,
            vec![
                reference("unit", "unit-local")?,
                reference("evidence", "evidence-local")?,
            ],
        )?;
        let snapshot = IdentityNode::new(
            "snapshot-local",
            IdentityKind::Snapshot,
            value(r#"{"fdirVersion":"2.1.0"}"#)?,
            vec![
                reference("artifact", "artifact-local")?,
                reference("assertion", "assertion-local")?,
            ],
        )?
        .with_operational_metadata(value(metadata)?);
        Ok(IdentityDag::new(vec![
            snapshot, assertion, evidence, unit, artifact,
        ])?)
    }

    #[test]
    fn computes_dependency_first_domain_separated_identities()
    -> Result<(), Box<dyn std::error::Error>> {
        let result = example_dag(
            r#"{"predicate":"text","value":"hello"}"#,
            r#"{"builtAt":1}"#,
        )?
        .compute()?;
        let artifact_position = result
            .order()
            .iter()
            .position(|key| key == "artifact-local");
        let assertion_position = result
            .order()
            .iter()
            .position(|key| key == "assertion-local");
        assert!(matches!(
            (artifact_position, assertion_position),
            (Some(artifact), Some(assertion)) if artifact < assertion
        ));
        let artifact = result.digest("artifact-local");
        let unit = result.digest("unit-local");
        assert_ne!(
            artifact.map(super::IdentityDigest::as_str),
            unit.map(super::IdentityDigest::as_str)
        );
        assert!(result.digest("snapshot-local").is_some());
        Ok(())
    }

    #[test]
    fn stable_changes_propagate_and_operational_metadata_does_not()
    -> Result<(), Box<dyn std::error::Error>> {
        let first = example_dag(
            r#"{"predicate":"text","value":"hello"}"#,
            r#"{"builtAt":1,"index":"first"}"#,
        )?
        .compute()?;
        let metadata_only = example_dag(
            r#"{"predicate":"text","value":"hello"}"#,
            r#"{"builtAt":2,"index":"rebuilt"}"#,
        )?
        .compute()?;
        let changed = example_dag(
            r#"{"predicate":"text","value":"changed"}"#,
            r#"{"builtAt":2,"index":"rebuilt"}"#,
        )?
        .compute()?;

        assert_eq!(
            first
                .digest("snapshot-local")
                .map(super::IdentityDigest::as_str),
            metadata_only
                .digest("snapshot-local")
                .map(super::IdentityDigest::as_str)
        );
        assert_ne!(
            first
                .digest("assertion-local")
                .map(super::IdentityDigest::as_str),
            changed
                .digest("assertion-local")
                .map(super::IdentityDigest::as_str)
        );
        assert_ne!(
            first
                .digest("snapshot-local")
                .map(super::IdentityDigest::as_str),
            changed
                .digest("snapshot-local")
                .map(super::IdentityDigest::as_str)
        );
        assert_eq!(
            first
                .digest("artifact-local")
                .map(super::IdentityDigest::as_str),
            changed
                .digest("artifact-local")
                .map(super::IdentityDigest::as_str)
        );
        Ok(())
    }

    #[test]
    fn rejects_cycles_before_kind_order() -> Result<(), Box<dyn std::error::Error>> {
        let first = IdentityNode::new(
            "first",
            IdentityKind::Snapshot,
            value("{}")?,
            vec![reference("next", "second")?],
        )?;
        let second = IdentityNode::new(
            "second",
            IdentityKind::Snapshot,
            value("{}")?,
            vec![reference("next", "first")?],
        )?;
        let error = IdentityDag::new(vec![first, second])?.compute();
        assert_eq!(
            error.as_ref().err().map(super::IdentityError::code),
            Some("FDIR-IDENTITY-DAG-CYCLE")
        );
        Ok(())
    }

    #[test]
    fn rejects_missing_targets_and_forward_identity_edges() -> Result<(), Box<dyn std::error::Error>>
    {
        let missing = IdentityNode::new(
            "artifact",
            IdentityKind::Artifact,
            value("{}")?,
            vec![reference("missing", "unknown")?],
        )?;
        let missing_error = IdentityDag::new(vec![missing]);
        assert_eq!(
            missing_error.as_ref().err().map(super::IdentityError::code),
            Some("FDIR-IDENTITY-MISSING-TARGET")
        );

        let snapshot =
            IdentityNode::new("snapshot", IdentityKind::Snapshot, value("{}")?, Vec::new())?;
        let artifact = IdentityNode::new(
            "artifact",
            IdentityKind::Artifact,
            value("{}")?,
            vec![reference("future", "snapshot")?],
        )?;
        let order_error = IdentityDag::new(vec![artifact, snapshot])?.compute();
        assert_eq!(
            order_error.as_ref().err().map(super::IdentityError::code),
            Some("FDIR-IDENTITY-DAG-ORDER")
        );
        Ok(())
    }
}
