#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::str;

use fdir_canonical::{CANONICAL_JSON_VERSION, CanonicalError, canonical_bytes};
use fdir_core::{CanonicalValue, Digest, ObjectValue, ResultState};

use crate::diagnostic::{StorageDiagnostic, StorageError, ValidationReport};
use crate::version::{
    SNAPSHOT_SCHEMA, SNAPSHOT_VERSION, negotiate_snapshot_version, require_supported,
};

const TOP_LEVEL_FIELDS: [&str; 9] = [
    "canonicalJson",
    "extensions",
    "objects",
    "payload",
    "provenance",
    "references",
    "schema",
    "statusTransitions",
    "version",
];
const OBJECT_FIELDS: [&str; 4] = ["byteLength", "digest", "mediaType", "role"];
const REFERENCE_FIELDS: [&str; 3] = ["relation", "source", "target"];
const TRANSITION_FIELDS: [&str; 2] = ["from", "to"];

/// One content-addressed evidence or auxiliary object referenced by a snapshot.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObjectDescriptor {
    digest: Digest,
    byte_length: u64,
    media_type: String,
    role: String,
}

impl ObjectDescriptor {
    /// Construct an object descriptor without reading or trusting object bytes.
    pub fn new(
        digest: Digest,
        byte_length: u64,
        media_type: impl Into<String>,
        role: impl Into<String>,
    ) -> Result<Self, StorageError> {
        Self::new_at(digest, byte_length, media_type, role, "$objects")
    }

    fn new_at(
        digest: Digest,
        byte_length: u64,
        media_type: impl Into<String>,
        role: impl Into<String>,
        path: &str,
    ) -> Result<Self, StorageError> {
        let media_type = media_type.into();
        let role = role.into();
        validate_text(&media_type, &format!("{path}/mediaType"), "media type")?;
        validate_text(&role, &format!("{path}/role"), "object role")?;
        Ok(Self {
            digest,
            byte_length,
            media_type,
            role,
        })
    }

    /// Borrow the independently verifiable object digest.
    #[must_use]
    pub const fn digest(&self) -> &Digest {
        &self.digest
    }

    /// Declared object length in bytes.
    #[must_use]
    pub const fn byte_length(&self) -> u64 {
        self.byte_length
    }

    /// Borrow the media type retained in the snapshot authority.
    #[must_use]
    pub fn media_type(&self) -> &str {
        &self.media_type
    }

    /// Borrow the semantic role retained in the snapshot authority.
    #[must_use]
    pub fn role(&self) -> &str {
        &self.role
    }

    fn to_value(&self) -> Result<CanonicalValue, StorageError> {
        let mut value = ObjectValue::new();
        value.insert(
            "byteLength".to_owned(),
            canonical_u64(self.byte_length, "$/objects/byteLength")?,
        );
        value.insert(
            "digest".to_owned(),
            CanonicalValue::String(self.digest.as_str().to_owned()),
        );
        value.insert(
            "mediaType".to_owned(),
            CanonicalValue::String(self.media_type.clone()),
        );
        value.insert(
            "role".to_owned(),
            CanonicalValue::String(self.role.clone()),
        );
        Ok(CanonicalValue::Object(value))
    }
}

/// Source node for one snapshot object-reference edge.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum ReferenceSource {
    /// The snapshot authority itself references the target object.
    Snapshot,
    /// One stored object references another stored object.
    Object(Digest),
}

impl ReferenceSource {
    fn as_string(&self) -> String {
        match self {
            Self::Snapshot => "snapshot".to_owned(),
            Self::Object(digest) => digest.as_str().to_owned(),
        }
    }
}

/// One typed edge in the snapshot's content-reference graph.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObjectReference {
    source: ReferenceSource,
    target: Digest,
    relation: String,
}

impl ObjectReference {
    /// Construct a typed reference edge.
    pub fn new(
        source: ReferenceSource,
        target: Digest,
        relation: impl Into<String>,
    ) -> Result<Self, StorageError> {
        Self::new_at(source, target, relation, "$/references")
    }

    fn new_at(
        source: ReferenceSource,
        target: Digest,
        relation: impl Into<String>,
        path: &str,
    ) -> Result<Self, StorageError> {
        let relation = relation.into();
        validate_text(&relation, &format!("{path}/relation"), "reference relation")?;
        Ok(Self {
            source,
            target,
            relation,
        })
    }

    /// Borrow the reference source.
    #[must_use]
    pub const fn source(&self) -> &ReferenceSource {
        &self.source
    }

    /// Borrow the reference target digest.
    #[must_use]
    pub const fn target(&self) -> &Digest {
        &self.target
    }

    /// Borrow the stable relation name.
    #[must_use]
    pub fn relation(&self) -> &str {
        &self.relation
    }

    fn to_value(&self) -> CanonicalValue {
        let mut value = ObjectValue::new();
        value.insert(
            "relation".to_owned(),
            CanonicalValue::String(self.relation.clone()),
        );
        value.insert(
            "source".to_owned(),
            CanonicalValue::String(self.source.as_string()),
        );
        value.insert(
            "target".to_owned(),
            CanonicalValue::String(self.target.as_str().to_owned()),
        );
        CanonicalValue::Object(value)
    }
}

/// One explicit and validated operation-state transition retained in a snapshot.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StatusTransition {
    from: ResultState,
    to: ResultState,
}

impl StatusTransition {
    /// Construct a state transition accepted by the frozen monotonic transition policy.
    pub fn new(from: ResultState, to: ResultState) -> Result<Self, StorageError> {
        Self::new_at(from, to, "$/statusTransitions")
    }

    fn new_at(from: ResultState, to: ResultState, path: &str) -> Result<Self, StorageError> {
        if !is_allowed_status_transition(from, to) {
            return Err(StorageError::new(
                "FDIR-SNAPSHOT-STATUS-TRANSITION",
                path,
                format!(
                    "status transition from {} to {} is not allowed",
                    from.as_str(),
                    to.as_str()
                ),
            ));
        }
        Ok(Self { from, to })
    }

    /// Source state.
    #[must_use]
    pub const fn from(self) -> ResultState {
        self.from
    }

    /// Target state.
    #[must_use]
    pub const fn to(self) -> ResultState {
        self.to
    }

    fn to_value(self) -> CanonicalValue {
        let mut value = ObjectValue::new();
        value.insert(
            "from".to_owned(),
            CanonicalValue::String(self.from.as_str().to_owned()),
        );
        value.insert(
            "to".to_owned(),
            CanonicalValue::String(self.to.as_str().to_owned()),
        );
        CanonicalValue::Object(value)
    }
}

/// Whether a transition preserves the frozen non-success and completion semantics.
#[must_use]
pub const fn is_allowed_status_transition(from: ResultState, to: ResultState) -> bool {
    match from {
        ResultState::Incomplete | ResultState::Unresolved => matches!(
            to,
            ResultState::Complete
                | ResultState::Partial
                | ResultState::Unsupported
                | ResultState::Cancelled
                | ResultState::Failed
                | ResultState::Unreadable
                | ResultState::ResourceLimited
                | ResultState::PolicyExcluded
        ),
        ResultState::Partial => matches!(
            to,
            ResultState::Complete
                | ResultState::Cancelled
                | ResultState::Failed
                | ResultState::ResourceLimited
        ),
        ResultState::ResourceLimited => matches!(
            to,
            ResultState::Complete
                | ResultState::Partial
                | ResultState::Cancelled
                | ResultState::Failed
        ),
        ResultState::Complete
        | ResultState::Unsupported
        | ResultState::Cancelled
        | ResultState::Failed
        | ResultState::Unreadable
        | ResultState::PolicyExcluded => false,
    }
}

/// Current authoritative snapshot container.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SnapshotManifest {
    payload: CanonicalValue,
    provenance: CanonicalValue,
    objects: Vec<ObjectDescriptor>,
    references: Vec<ObjectReference>,
    status_transitions: Vec<StatusTransition>,
    extensions: ObjectValue,
}

impl SnapshotManifest {
    /// Construct a current snapshot and normalize order-insensitive collections.
    #[must_use]
    pub fn new(
        payload: CanonicalValue,
        provenance: CanonicalValue,
        mut objects: Vec<ObjectDescriptor>,
        mut references: Vec<ObjectReference>,
        mut status_transitions: Vec<StatusTransition>,
        extensions: ObjectValue,
    ) -> Self {
        objects.sort_by(|left, right| {
            (
                left.digest.as_str(),
                left.role.as_str(),
                left.media_type.as_str(),
                left.byte_length,
            )
                .cmp(&(
                    right.digest.as_str(),
                    right.role.as_str(),
                    right.media_type.as_str(),
                    right.byte_length,
                ))
        });
        references.sort_by(|left, right| {
            (
                left.source.as_string(),
                left.target.as_str(),
                left.relation.as_str(),
            )
                .cmp(&(
                    right.source.as_string(),
                    right.target.as_str(),
                    right.relation.as_str(),
                ))
        });
        status_transitions.sort_by(|left, right| {
            (left.from.as_str(), left.to.as_str()).cmp(&(right.from.as_str(), right.to.as_str()))
        });
        Self {
            payload,
            provenance,
            objects,
            references,
            status_transitions,
            extensions,
        }
    }

    /// Borrow the complete FDIR payload authority without projecting it.
    #[must_use]
    pub const fn payload(&self) -> &CanonicalValue {
        &self.payload
    }

    /// Borrow provenance retained independently from payload truth.
    #[must_use]
    pub const fn provenance(&self) -> &CanonicalValue {
        &self.provenance
    }

    /// Borrow content-addressed object descriptors.
    #[must_use]
    pub fn objects(&self) -> &[ObjectDescriptor] {
        &self.objects
    }

    /// Borrow the complete typed object-reference graph.
    #[must_use]
    pub fn references(&self) -> &[ObjectReference] {
        &self.references
    }

    /// Borrow explicit operation-state transitions.
    #[must_use]
    pub fn status_transitions(&self) -> &[StatusTransition] {
        &self.status_transitions
    }

    /// Borrow retained extension members.
    #[must_use]
    pub const fn extensions(&self) -> &ObjectValue {
        &self.extensions
    }

    /// Validate structural and semantic invariants without reading the object store.
    #[must_use]
    pub fn validation_report(&self) -> ValidationReport {
        let mut report = ValidationReport::new();
        if self.payload.as_object().is_none() {
            report.push(StorageDiagnostic::new(
                "FDIR-SNAPSHOT-PAYLOAD-TYPE",
                "$/payload",
                "snapshot payload must be an object",
            ));
        }
        match self.provenance.as_object() {
            Some(value) if value.is_empty() => report.push(StorageDiagnostic::new(
                "FDIR-SNAPSHOT-PROVENANCE-EMPTY",
                "$/provenance",
                "snapshot provenance must not be empty",
            )),
            Some(_) => {}
            None => report.push(StorageDiagnostic::new(
                "FDIR-SNAPSHOT-PROVENANCE-TYPE",
                "$/provenance",
                "snapshot provenance must be an object",
            )),
        }

        let mut declared = BTreeSet::new();
        for (index, descriptor) in self.objects.iter().enumerate() {
            if !declared.insert(descriptor.digest.clone()) {
                report.push(StorageDiagnostic::new(
                    "FDIR-SNAPSHOT-OBJECT-DUPLICATE",
                    format!("$/objects/{index}/digest"),
                    format!("duplicate object descriptor for {}", descriptor.digest),
                ));
            }
        }

        let mut reference_keys = BTreeSet::new();
        let mut root_targets = BTreeSet::new();
        let mut adjacency: BTreeMap<Digest, BTreeSet<Digest>> = declared
            .iter()
            .cloned()
            .map(|digest| (digest, BTreeSet::new()))
            .collect();
        let mut indegree: BTreeMap<Digest, usize> =
            declared.iter().cloned().map(|digest| (digest, 0)).collect();

        for (index, reference) in self.references.iter().enumerate() {
            let key = (
                reference.source.as_string(),
                reference.target.as_str().to_owned(),
                reference.relation.clone(),
            );
            if !reference_keys.insert(key) {
                report.push(StorageDiagnostic::new(
                    "FDIR-SNAPSHOT-REFERENCE-DUPLICATE",
                    format!("$/references/{index}"),
                    "duplicate object-reference edge",
                ));
                continue;
            }
            if !declared.contains(&reference.target) {
                report.push(StorageDiagnostic::new(
                    "FDIR-SNAPSHOT-REFERENCE-TARGET",
                    format!("$/references/{index}/target"),
                    format!("reference target {} is not declared", reference.target),
                ));
                continue;
            }
            match &reference.source {
                ReferenceSource::Snapshot => {
                    root_targets.insert(reference.target.clone());
                }
                ReferenceSource::Object(source) => {
                    if !declared.contains(source) {
                        report.push(StorageDiagnostic::new(
                            "FDIR-SNAPSHOT-REFERENCE-SOURCE",
                            format!("$/references/{index}/source"),
                            format!("reference source {source} is not declared"),
                        ));
                        continue;
                    }
                    if let Some(targets) = adjacency.get_mut(source)
                        && targets.insert(reference.target.clone())
                        && let Some(value) = indegree.get_mut(&reference.target)
                    {
                        *value = value.saturating_add(1);
                    }
                }
            }
        }

        let mut queue: VecDeque<Digest> = indegree
            .iter()
            .filter(|(_, value)| **value == 0)
            .map(|(digest, _)| digest.clone())
            .collect();
        let mut visited = 0_usize;
        while let Some(source) = queue.pop_front() {
            visited = visited.saturating_add(1);
            if let Some(targets) = adjacency.get(&source) {
                for target in targets {
                    if let Some(value) = indegree.get_mut(target) {
                        *value = value.saturating_sub(1);
                        if *value == 0 {
                            queue.push_back(target.clone());
                        }
                    }
                }
            }
        }
        if visited != declared.len() {
            report.push(StorageDiagnostic::new(
                "FDIR-SNAPSHOT-REFERENCE-CYCLE",
                "$/references",
                "object-reference graph contains a cycle",
            ));
        }

        let mut reachable = BTreeSet::new();
        let mut queue: VecDeque<Digest> = root_targets.into_iter().collect();
        while let Some(source) = queue.pop_front() {
            if !reachable.insert(source.clone()) {
                continue;
            }
            if let Some(targets) = adjacency.get(&source) {
                queue.extend(targets.iter().cloned());
            }
        }
        for digest in declared.difference(&reachable) {
            report.push(StorageDiagnostic::new(
                "FDIR-SNAPSHOT-OBJECT-UNREFERENCED",
                "$/objects",
                format!("declared object {digest} is not reachable from the snapshot"),
            ));
        }

        let mut transition_keys = BTreeSet::new();
        for (index, transition) in self.status_transitions.iter().enumerate() {
            if !is_allowed_status_transition(transition.from, transition.to) {
                report.push(StorageDiagnostic::new(
                    "FDIR-SNAPSHOT-STATUS-TRANSITION",
                    format!("$/statusTransitions/{index}"),
                    format!(
                        "status transition from {} to {} is not allowed",
                        transition.from.as_str(),
                        transition.to.as_str()
                    ),
                ));
            }
            if !transition_keys.insert((transition.from.as_str(), transition.to.as_str())) {
                report.push(StorageDiagnostic::new(
                    "FDIR-SNAPSHOT-STATUS-DUPLICATE",
                    format!("$/statusTransitions/{index}"),
                    "duplicate status transition",
                ));
            }
        }
        report
    }

    /// Serialize the exact byte-stable canonical snapshot authority.
    pub fn canonical_bytes(&self) -> Result<Vec<u8>, StorageError> {
        self.validation_report().into_result()?;
        canonical_bytes(&self.to_value()?).map_err(canonical_storage_error)
    }

    fn to_value(&self) -> Result<CanonicalValue, StorageError> {
        let mut value = ObjectValue::new();
        value.insert(
            "canonicalJson".to_owned(),
            CanonicalValue::String(CANONICAL_JSON_VERSION.to_owned()),
        );
        value.insert(
            "extensions".to_owned(),
            CanonicalValue::Object(self.extensions.clone()),
        );
        value.insert(
            "objects".to_owned(),
            CanonicalValue::Array(
                self.objects
                    .iter()
                    .map(ObjectDescriptor::to_value)
                    .collect::<Result<Vec<_>, _>>()?,
            ),
        );
        value.insert("payload".to_owned(), self.payload.clone());
        value.insert("provenance".to_owned(), self.provenance.clone());
        value.insert(
            "references".to_owned(),
            CanonicalValue::Array(
                self.references
                    .iter()
                    .map(ObjectReference::to_value)
                    .collect(),
            ),
        );
        value.insert(
            "schema".to_owned(),
            CanonicalValue::String(SNAPSHOT_SCHEMA.to_owned()),
        );
        value.insert(
            "statusTransitions".to_owned(),
            CanonicalValue::Array(
                self.status_transitions
                    .iter()
                    .copied()
                    .map(StatusTransition::to_value)
                    .collect(),
            ),
        );
        value.insert(
            "version".to_owned(),
            canonical_u64(SNAPSHOT_VERSION, "$/version")?,
        );
        Ok(CanonicalValue::Object(value))
    }
}

/// Parse, version-negotiate, normalize, and validate canonical snapshot bytes.
pub fn parse_snapshot_bytes(bytes: &[u8]) -> Result<SnapshotManifest, StorageError> {
    let input = str::from_utf8(bytes).map_err(|error| {
        StorageError::new(
            "FDIR-SNAPSHOT-UTF8",
            "$",
            format!("snapshot is not valid UTF-8: {error}"),
        )
    })?;
    let value = CanonicalValue::parse_json(input).map_err(|error| {
        StorageError::new(
            "FDIR-SNAPSHOT-JSON",
            "$",
            format!("snapshot JSON could not be parsed: {error}"),
        )
    })?;
    let canonical = canonical_bytes(&value).map_err(canonical_storage_error)?;
    if canonical != bytes {
        return Err(StorageError::new(
            "FDIR-SNAPSHOT-NONCANONICAL",
            "$",
            "snapshot bytes are not the exact canonical JSON spelling",
        ));
    }
    let object = value.as_object().ok_or_else(|| {
        StorageError::new(
            "FDIR-SNAPSHOT-TYPE",
            "$",
            "snapshot container must be an object",
        )
    })?;

    let schema = required_string(object, "schema", "$/schema")?;
    let version = required_u64(object, "version", "$/version")?;
    let canonical_json = required_string(object, "canonicalJson", "$/canonicalJson")?;
    require_supported(negotiate_snapshot_version(
        schema,
        version,
        canonical_json,
    ))?;
    reject_unknown_fields(object, &TOP_LEVEL_FIELDS, "$", "snapshot")?;

    let payload = required_field(object, "payload", "$/payload")?.clone();
    let provenance = required_field(object, "provenance", "$/provenance")?.clone();
    let extensions = required_object(object, "extensions", "$/extensions")?.clone();

    let objects = required_array(object, "objects", "$/objects")?
        .iter()
        .enumerate()
        .map(|(index, item)| parse_object_descriptor(item, index))
        .collect::<Result<Vec<_>, _>>()?;
    let references = required_array(object, "references", "$/references")?
        .iter()
        .enumerate()
        .map(|(index, item)| parse_reference(item, index))
        .collect::<Result<Vec<_>, _>>()?;
    let status_transitions = required_array(
        object,
        "statusTransitions",
        "$/statusTransitions",
    )?
    .iter()
    .enumerate()
    .map(|(index, item)| parse_transition(item, index))
    .collect::<Result<Vec<_>, _>>()?;

    let manifest = SnapshotManifest::new(
        payload,
        provenance,
        objects,
        references,
        status_transitions,
        extensions,
    );
    manifest.validation_report().into_result()?;
    if manifest.canonical_bytes()? != bytes {
        return Err(StorageError::new(
            "FDIR-SNAPSHOT-NONCANONICAL-ORDER",
            "$",
            "order-insensitive snapshot collections are not in deterministic order",
        ));
    }
    Ok(manifest)
}

fn parse_object_descriptor(
    value: &CanonicalValue,
    index: usize,
) -> Result<ObjectDescriptor, StorageError> {
    let path = format!("$/objects/{index}");
    let object = value.as_object().ok_or_else(|| {
        StorageError::new(
            "FDIR-SNAPSHOT-OBJECT-TYPE",
            &path,
            "object descriptor must be an object",
        )
    })?;
    reject_unknown_fields(object, &OBJECT_FIELDS, &path, "object descriptor")?;
    let digest_text = required_string(object, "digest", &format!("{path}/digest"))?;
    let digest = Digest::new(digest_text.to_owned()).map_err(|error| {
        StorageError::new(
            "FDIR-SNAPSHOT-OBJECT-DIGEST",
            format!("{path}/digest"),
            error.to_string(),
        )
    })?;
    ObjectDescriptor::new_at(
        digest,
        required_u64(object, "byteLength", &format!("{path}/byteLength"))?,
        required_string(object, "mediaType", &format!("{path}/mediaType"))?,
        required_string(object, "role", &format!("{path}/role"))?,
        &path,
    )
}

fn parse_reference(
    value: &CanonicalValue,
    index: usize,
) -> Result<ObjectReference, StorageError> {
    let path = format!("$/references/{index}");
    let object = value.as_object().ok_or_else(|| {
        StorageError::new(
            "FDIR-SNAPSHOT-REFERENCE-TYPE",
            &path,
            "reference entry must be an object",
        )
    })?;
    reject_unknown_fields(object, &REFERENCE_FIELDS, &path, "reference")?;
    let source_text = required_string(object, "source", &format!("{path}/source"))?;
    let source = if source_text == "snapshot" {
        ReferenceSource::Snapshot
    } else {
        ReferenceSource::Object(Digest::new(source_text.to_owned()).map_err(|error| {
            StorageError::new(
                "FDIR-SNAPSHOT-REFERENCE-SOURCE",
                format!("{path}/source"),
                error.to_string(),
            )
        })?)
    };
    let target = Digest::new(
        required_string(object, "target", &format!("{path}/target"))?.to_owned(),
    )
    .map_err(|error| {
        StorageError::new(
            "FDIR-SNAPSHOT-REFERENCE-TARGET",
            format!("{path}/target"),
            error.to_string(),
        )
    })?;
    ObjectReference::new_at(
        source,
        target,
        required_string(object, "relation", &format!("{path}/relation"))?,
        &path,
    )
}

fn parse_transition(
    value: &CanonicalValue,
    index: usize,
) -> Result<StatusTransition, StorageError> {
    let path = format!("$/statusTransitions/{index}");
    let object = value.as_object().ok_or_else(|| {
        StorageError::new(
            "FDIR-SNAPSHOT-STATUS-TYPE",
            &path,
            "status transition must be an object",
        )
    })?;
    reject_unknown_fields(object, &TRANSITION_FIELDS, &path, "status transition")?;
    let from = parse_result_state(
        required_string(object, "from", &format!("{path}/from"))?,
        &format!("{path}/from"),
    )?;
    let to = parse_result_state(
        required_string(object, "to", &format!("{path}/to"))?,
        &format!("{path}/to"),
    )?;
    StatusTransition::new_at(from, to, &path)
}

fn parse_result_state(value: &str, path: &str) -> Result<ResultState, StorageError> {
    ResultState::ALL
        .into_iter()
        .find(|state| state.as_str() == value)
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-SNAPSHOT-STATUS-VALUE",
                path,
                format!("unknown result state {value:?}"),
            )
        })
}

fn required_field<'a>(
    object: &'a ObjectValue,
    key: &str,
    path: &str,
) -> Result<&'a CanonicalValue, StorageError> {
    object.get(key).ok_or_else(|| {
        StorageError::new(
            "FDIR-SNAPSHOT-FIELD-MISSING",
            path,
            format!("required field {key:?} is missing"),
        )
    })
}

fn required_string<'a>(
    object: &'a ObjectValue,
    key: &str,
    path: &str,
) -> Result<&'a str, StorageError> {
    required_field(object, key, path)?
        .as_str()
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-SNAPSHOT-FIELD-TYPE",
                path,
                format!("field {key:?} must be a string"),
            )
        })
}

fn required_u64(object: &ObjectValue, key: &str, path: &str) -> Result<u64, StorageError> {
    required_field(object, key, path)?
        .as_number()
        .and_then(|number| number.as_u64())
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-SNAPSHOT-FIELD-TYPE",
                path,
                format!("field {key:?} must be a non-negative integer"),
            )
        })
}

fn required_object<'a>(
    object: &'a ObjectValue,
    key: &str,
    path: &str,
) -> Result<&'a ObjectValue, StorageError> {
    required_field(object, key, path)?
        .as_object()
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-SNAPSHOT-FIELD-TYPE",
                path,
                format!("field {key:?} must be an object"),
            )
        })
}

fn required_array<'a>(
    object: &'a ObjectValue,
    key: &str,
    path: &str,
) -> Result<&'a [CanonicalValue], StorageError> {
    required_field(object, key, path)?
        .as_array()
        .ok_or_else(|| {
            StorageError::new(
                "FDIR-SNAPSHOT-FIELD-TYPE",
                path,
                format!("field {key:?} must be an array"),
            )
        })
}

fn reject_unknown_fields(
    object: &ObjectValue,
    expected: &[&str],
    path: &str,
    label: &str,
) -> Result<(), StorageError> {
    for key in object.keys() {
        if !expected.contains(&key.as_str()) {
            return Err(StorageError::new(
                "FDIR-SNAPSHOT-FIELD-UNKNOWN",
                format!("{path}/{key}"),
                format!("unknown {label} field {key:?}"),
            ));
        }
    }
    Ok(())
}

fn canonical_u64(value: u64, path: &str) -> Result<CanonicalValue, StorageError> {
    CanonicalValue::parse_json(&value.to_string()).map_err(|error| {
        StorageError::new(
            "FDIR-SNAPSHOT-INTERNAL-NUMBER",
            path,
            format!("could not construct canonical integer: {error}"),
        )
    })
}

fn validate_text(value: &str, path: &str, label: &str) -> Result<(), StorageError> {
    if value.is_empty() {
        return Err(StorageError::new(
            "FDIR-SNAPSHOT-TEXT-EMPTY",
            path,
            format!("{label} must not be empty"),
        ));
    }
    if value.chars().any(char::is_control) {
        return Err(StorageError::new(
            "FDIR-SNAPSHOT-TEXT-CONTROL",
            path,
            format!("{label} must not contain control characters"),
        ));
    }
    Ok(())
}

fn canonical_storage_error(error: CanonicalError) -> StorageError {
    StorageError::new(error.code(), error.path(), error.message())
}
