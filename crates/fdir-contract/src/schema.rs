#![forbid(unsafe_code)]

/// Logical-model authority axis for one generated entity.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EntityDomain {
    Evidence,
    RecordedInformation,
    Shared,
    Root,
}

/// Machine property kind retained in the generated Rust descriptor registry.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PropertyKind {
    Id,
    IdArray,
    Reference,
    ReferenceArray,
    EntityArray,
    Enumeration,
    String,
    Constant,
    Integer,
    Number,
    NumberArray,
    Digest,
    Object,
    Any,
}

/// One property copied mechanically from `machine/logical-model.yaml`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PropertySpec {
    pub json_name: &'static str,
    pub rust_name: &'static str,
    pub kind: PropertyKind,
    pub required: bool,
    pub target: Option<&'static str>,
    pub enumeration: Option<&'static str>,
    pub constant: Option<&'static str>,
    pub minimum: Option<f64>,
    pub maximum: Option<f64>,
    pub min_items: Option<usize>,
    pub max_items: Option<usize>,
}

/// One generated entity contract.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EntitySpec {
    pub name: &'static str,
    pub domain: EntityDomain,
    pub identity: &'static str,
    pub properties: &'static [PropertySpec],
}

/// One generated closed enumeration contract.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EnumSpec {
    pub name: &'static str,
    pub values: &'static [&'static str],
}

/// Find an entity descriptor without introducing a second handwritten registry.
pub fn entity_spec(name: &str) -> Option<&'static EntitySpec> {
    crate::ENTITY_SPECS.iter().find(|item| item.name == name)
}

/// Find a closed enumeration descriptor.
pub fn enum_spec(name: &str) -> Option<&'static EnumSpec> {
    crate::ENUM_SPECS.iter().find(|item| item.name == name)
}
