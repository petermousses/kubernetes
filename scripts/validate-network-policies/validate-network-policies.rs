use serde::Deserialize;
use serde_yaml::{Deserializer, Value};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const BASELINE_RESOURCE: &str = "../_shared/network-policy/baseline";
const DNS_RESOURCE: &str = "../_shared/network-policy/dns-egress";
const INGRESS_SECURITY_RESOURCE: &str = "../_shared/ingress-security";
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
const INGRESS_SECURITY_APPS: &[&str] = &[
    "board-games",
    "crafty",
    "external-routes",
    "homepage",
    "immich",
    "it-tools",
    "jellyfin",
    "kiwix",
    "librechat",
    "monitoring",
    "n8n",
    "open-speed-test",
    "open-webui",
    "paperless-ngx",
    "qr-code-generator",
    "searxng",
    "syncthing",
    "text2shop",
    "vaultwarden",
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

fn matching_resources<'a>(
    documents: &'a [Value],
    api_version: &str,
    kind: &str,
    name: &str,
) -> Vec<&'a Value> {
    documents
        .iter()
        .filter(|document| {
            string_at(document, &["apiVersion"]).as_deref() == Some(api_version)
                && string_at(document, &["kind"]).as_deref() == Some(kind)
                && string_at(document, &["metadata", "name"]).as_deref() == Some(name)
        })
        .collect()
}

fn ingress_routes_have_security_headers(document: &Value) -> bool {
    value_at_path(document, &["spec", "routes"])
        .and_then(Value::as_sequence)
        .is_some_and(|routes| {
            !routes.is_empty()
                && routes.iter().all(|route| {
                    value_at(route, "middlewares")
                        .and_then(Value::as_sequence)
                        .is_some_and(|middlewares| {
                            middlewares.iter().any(|middleware| {
                                string_at(middleware, &["name"]).as_deref()
                                    == Some("security-headers")
                            })
                        })
                })
        })
}

fn validate_admission_policies(root: &Path, errors: &mut Vec<String>) {
    let documents = match render(&root.join("platform/policies")) {
        Ok(documents) => documents,
        Err(error) => {
            errors.push(error);
            return;
        }
    };

    for binding_name in [
        "workload-metadata.platform.mousses.xyz",
        "workload-host-isolation.platform.mousses.xyz",
    ] {
        let bindings = matching_resources(
            &documents,
            "admissionregistration.k8s.io/v1",
            "ValidatingAdmissionPolicyBinding",
            binding_name,
        );
        if bindings.len() != 1 {
            errors.push(format!(
                "admission policy binding {} must render exactly once",
                binding_name
            ));
            continue;
        }
        let binding = bindings[0];
        let actions = value_at_path(binding, &["spec", "validationActions"])
            .and_then(Value::as_sequence)
            .map(|actions| actions.iter().filter_map(Value::as_str).collect::<Vec<_>>());
        if actions.as_deref() != Some(&["Warn", "Audit"]) {
            errors.push(format!(
                "admission policy binding {} must remain audit-and-warn only",
                binding_name
            ));
        }
        if string_at(
            binding,
            &[
                "spec",
                "matchResources",
                "namespaceSelector",
                "matchLabels",
                "platform.mousses.xyz/admission",
            ],
        )
        .as_deref()
            != Some("audit")
        {
            errors.push(format!(
                "admission policy binding {} must target opted-in audit namespaces",
                binding_name
            ));
        }
    }
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
    let expected_security_headers_spec: Value = serde_yaml::from_str(
        "headers:\n  contentTypeNosniff: true\n  referrerPolicy: strict-origin-when-cross-origin\n",
    )
    .expect("security headers spec is valid YAML");
    let mut errors = Vec::new();

    validate_admission_policies(&root, &mut errors);

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
        let uses_ingress_security = INGRESS_SECURITY_APPS.contains(&app);

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
        if resources.contains(&INGRESS_SECURITY_RESOURCE) != uses_ingress_security {
            errors.push(format!(
                "{}: shared ingress-security reference must be {}",
                app,
                if uses_ingress_security {
                    "present"
                } else {
                    "absent"
                }
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

        if uses_ingress_security {
            let middleware = matching_resources(
                &documents,
                "traefik.io/v1alpha1",
                "Middleware",
                "security-headers",
            );
            if middleware.len() != 1 {
                errors.push(format!(
                    "{}: expected one shared security-headers middleware, rendered {}",
                    app,
                    middleware.len()
                ));
            } else if string_at(middleware[0], &["metadata", "namespace"]).as_deref()
                != expected_namespace.as_deref()
            {
                errors.push(format!(
                    "{}: security-headers rendered in {:?}",
                    app,
                    string_at(middleware[0], &["metadata", "namespace"])
                ));
            }
            if middleware.len() == 1
                && value_at_path(middleware[0], &["spec"]) != Some(&expected_security_headers_spec)
            {
                errors.push(format!("{}: security-headers spec changed", app));
            }

            let ingress_documents: Vec<_> = documents
                .iter()
                .filter(|document| {
                    string_at(document, &["apiVersion"]).as_deref() == Some("networking.k8s.io/v1")
                        && string_at(document, &["kind"]).as_deref() == Some("Ingress")
                })
                .collect();
            let expected_middleware = format!(
                "{}-security-headers@kubernetescrd",
                expected_namespace.as_deref().unwrap_or_default()
            );
            if ingress_documents.iter().any(|ingress| {
                string_at(
                    ingress,
                    &[
                        "metadata",
                        "annotations",
                        "traefik.ingress.kubernetes.io/router.middlewares",
                    ],
                )
                .as_deref()
                    != Some(expected_middleware.as_str())
            }) {
                errors.push(format!(
                    "{}: every Ingress must use its namespace-local security-headers middleware",
                    app
                ));
            }

            let ingress_routes: Vec<_> = documents
                .iter()
                .filter(|document| {
                    string_at(document, &["apiVersion"]).as_deref() == Some("traefik.io/v1alpha1")
                        && string_at(document, &["kind"]).as_deref() == Some("IngressRoute")
                })
                .collect();
            if ingress_routes
                .iter()
                .any(|route| !ingress_routes_have_security_headers(route))
            {
                errors.push(format!(
                    "{}: every IngressRoute rule must use security-headers",
                    app
                ));
            }
        }
    }

    if errors.is_empty() {
        println!(
            "validated {} app kustomizations and shared platform invariants",
            app_directories.len()
        );
    } else {
        for error in errors {
            eprintln!("- {}", error);
        }
        std::process::exit(1);
    }
}
