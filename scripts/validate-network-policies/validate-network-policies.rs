use serde::Deserialize;
use serde_yaml::{Deserializer, Value};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const BASELINE_RESOURCE: &str = "../_shared/network-policy/baseline";
const DNS_RESOURCE: &str = "../_shared/network-policy/dns-egress";
const BASELINE_EXCEPTIONS: &[&str] = &["rancher"];
// Monitoring keeps HelmChart objects in kube-system and targets workloads to its app namespace.
const NAMESPACE_TRANSFORM_EXCEPTIONS: &[&str] = &["monitoring"];
const SHARED_DNS_APPS: &[&str] = &[
    "immich",
    "librechat",
    "monitoring",
    "paperless-ngx",
    "searxng",
];

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct ResourceIdentity(
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

fn value_at<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    let key = Value::String(key.to_owned());
    value.as_mapping()?.get(&key)
}

fn value_at_path<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    path.iter()
        .try_fold(value, |current, key| value_at(current, key))
}

fn string_at(value: &Value, path: &[&str]) -> Option<String> {
    value_at_path(value, path)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
}

fn parse_documents(path: &Path, contents: &str) -> Result<Vec<Value>, String> {
    Deserializer::from_str(contents)
        .enumerate()
        .map(|(index, document)| {
            let value = Value::deserialize(document).map_err(|error| {
                format!(
                    "failed to parse YAML document {} from {}: {}",
                    index + 1,
                    path.display(),
                    error
                )
            })?;
            Ok(value)
        })
        .filter(|result| match result {
            Ok(value) => !value.is_null(),
            Err(_) => true,
        })
        .collect()
}

fn load_yaml(path: &Path) -> Result<Value, String> {
    let contents = fs::read_to_string(path)
        .map_err(|error| format!("failed to read {}: {}", path.display(), error))?;
    serde_yaml::from_str(&contents)
        .map_err(|error| format!("failed to parse {}: {}", path.display(), error))
}

fn render(path: &Path) -> Result<Vec<Value>, String> {
    let output = Command::new("kubectl")
        .args(["kustomize"])
        .arg(path)
        .output()
        .map_err(|error| {
            format!(
                "failed to run kubectl kustomize {}: {}",
                path.display(),
                error
            )
        })?;

    if !output.status.success() {
        return Err(format!(
            "kubectl kustomize {} failed:\n{}",
            path.display(),
            String::from_utf8_lossy(&output.stderr)
        ));
    }

    parse_documents(path, &String::from_utf8_lossy(&output.stdout))
}

fn network_policies<'a>(documents: &'a [Value], name: &str) -> Vec<&'a Value> {
    documents
        .iter()
        .filter(|document| {
            string_at(document, &["apiVersion"]).as_deref() == Some("networking.k8s.io/v1")
                && string_at(document, &["kind"]).as_deref() == Some("NetworkPolicy")
                && string_at(document, &["metadata", "name"]).as_deref() == Some(name)
        })
        .collect()
}

fn resource_identity(document: &Value) -> ResourceIdentity {
    ResourceIdentity(
        string_at(document, &["apiVersion"]),
        string_at(document, &["kind"]),
        string_at(document, &["metadata", "namespace"]),
        string_at(document, &["metadata", "name"]),
    )
}

fn repo_root() -> Result<PathBuf, String> {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .ok_or_else(|| {
            format!(
                "could not determine repository root from {}",
                manifest_dir.display()
            )
        })
}

fn main() {
    let root = repo_root().unwrap_or_else(|error| {
        eprintln!("- {error}");
        std::process::exit(1);
    });
    let apps_root = root.join("apps");
    let expected_baseline_spec: Value =
        serde_yaml::from_str("podSelector: {}\npolicyTypes:\n  - Ingress\n  - Egress\n")
            .expect("baseline spec is valid YAML");
    let mut errors = Vec::new();

    let mut app_directories: Vec<PathBuf> = fs::read_dir(&apps_root)
        .unwrap_or_else(|error| {
            eprintln!("- failed to read {}: {}", apps_root.display(), error);
            std::process::exit(1);
        })
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| {
            path.is_dir()
                && path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| !name.starts_with('_'))
                && path.join("kustomization.yaml").is_file()
        })
        .collect();
    app_directories.sort();

    match render(&apps_root.join("_shared/network-policy/baseline")) {
        Ok(documents) => {
            let baseline = network_policies(&documents, "default-deny-all");
            if baseline.len() != 1
                || value_at_path(baseline[0], &["spec"]) != Some(&expected_baseline_spec)
            {
                errors.push(
                    "shared baseline must render exactly one ingress-and-egress default-deny-all policy"
                        .to_owned(),
                );
            }
        }
        Err(error) => {
            errors.push(error);
        }
    }

    let expected_dns_spec = match render(&apps_root.join("_shared/network-policy/dns-egress")) {
        Ok(documents) => {
            let dns = network_policies(&documents, "allow-dns-egress");
            if dns.len() != 1 {
                errors.push(
                    "shared DNS profile must render exactly one allow-dns-egress policy".to_owned(),
                );
                None
            } else {
                value_at_path(dns[0], &["spec"]).cloned()
            }
        }
        Err(error) => {
            errors.push(error);
            None
        }
    };

    for directory in &app_directories {
        let app = directory
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("<unknown>");
        let kustomization_path = directory.join("kustomization.yaml");
        let namespace_path = directory.join("namespace.yaml");
        let kustomization = match load_yaml(&kustomization_path) {
            Ok(value) => value,
            Err(error) => {
                errors.push(error);
                continue;
            }
        };
        let namespace = match load_yaml(&namespace_path) {
            Ok(value) => value,
            Err(error) => {
                errors.push(error);
                continue;
            }
        };
        let resources = value_at_path(&kustomization, &["resources"])
            .and_then(Value::as_sequence)
            .map(|values| values.iter().filter_map(Value::as_str).collect::<Vec<_>>())
            .unwrap_or_default();
        let expected_namespace = string_at(&namespace, &["metadata", "name"]);
        let requires_baseline = !BASELINE_EXCEPTIONS.contains(&app);
        let uses_shared_dns = SHARED_DNS_APPS.contains(&app);

        if resources.contains(&BASELINE_RESOURCE) != requires_baseline {
            errors.push(format!(
                "{}: baseline reference must be {}",
                app,
                if requires_baseline {
                    "present"
                } else {
                    "absent"
                }
            ));
        }
        if resources.contains(&DNS_RESOURCE) != uses_shared_dns {
            errors.push(format!(
                "{}: shared DNS reference must be {}",
                app,
                if uses_shared_dns { "present" } else { "absent" }
            ));
        }
        if requires_baseline
            && !NAMESPACE_TRANSFORM_EXCEPTIONS.contains(&app)
            && string_at(&kustomization, &["namespace"]).as_deref() != expected_namespace.as_deref()
        {
            errors.push(format!(
                "{}: kustomization namespace must be {:?}",
                app, expected_namespace
            ));
        }

        let documents = match render(directory) {
            Ok(documents) => documents,
            Err(error) => {
                errors.push(error);
                continue;
            }
        };

        let mut identities = BTreeMap::new();
        for document in &documents {
            *identities.entry(resource_identity(document)).or_insert(0) += 1;
        }
        let duplicates: Vec<_> = identities
            .iter()
            .filter(|(_, count)| **count > 1)
            .map(|(identity, _)| identity)
            .collect();
        if !duplicates.is_empty() {
            errors.push(format!(
                "{}: duplicate rendered resource identities: {:?}",
                app, duplicates
            ));
        }

        let baseline = network_policies(&documents, "default-deny-all");
        let expected_count = usize::from(requires_baseline);
        if baseline.len() != expected_count {
            errors.push(format!(
                "{}: expected {} default-deny-all policy, rendered {}",
                app,
                expected_count,
                baseline.len()
            ));
        } else if requires_baseline {
            let policy = baseline[0];
            if string_at(policy, &["metadata", "namespace"]).as_deref()
                != expected_namespace.as_deref()
            {
                errors.push(format!(
                    "{}: default-deny-all rendered in {:?}",
                    app,
                    string_at(policy, &["metadata", "namespace"])
                ));
            }
            if value_at_path(policy, &["spec"]) != Some(&expected_baseline_spec) {
                errors.push(format!("{}: default-deny-all spec changed", app));
            }
        }

        if uses_shared_dns {
            let dns = network_policies(&documents, "allow-dns-egress");
            if dns.len() != 1 {
                errors.push(format!(
                    "{}: expected one shared allow-dns-egress policy, rendered {}",
                    app,
                    dns.len()
                ));
            } else {
                let policy = dns[0];
                if string_at(policy, &["metadata", "namespace"]).as_deref()
                    != expected_namespace.as_deref()
                {
                    errors.push(format!(
                        "{}: allow-dns-egress rendered in {:?}",
                        app,
                        string_at(policy, &["metadata", "namespace"])
                    ));
                }
                if expected_dns_spec.as_ref() != value_at_path(policy, &["spec"]) {
                    errors.push(format!("{}: shared allow-dns-egress spec changed", app));
                }
            }
        }
    }

    if errors.is_empty() {
        println!(
            "validated {} app kustomizations and shared network-policy invariants",
            app_directories.len()
        );
    } else {
        for error in errors {
            eprintln!("- {}", error);
        }
        std::process::exit(1);
    }
}
