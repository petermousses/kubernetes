#!/usr/bin/env ruby
# frozen_string_literal: true

require "open3"
require "yaml"

ROOT = File.expand_path("..", __dir__)
APPS_ROOT = File.join(ROOT, "apps")
BASELINE_RESOURCE = "../_shared/network-policy/baseline"
DNS_RESOURCE = "../_shared/network-policy/dns-egress"
BASELINE_EXCEPTIONS = ["rancher"].freeze
SHARED_DNS_APPS = %w[immich paperless-ngx searxng].freeze
EXPECTED_BASELINE_SPEC = {
  "podSelector" => {},
  "policyTypes" => %w[Ingress Egress]
}.freeze

def render(path)
  stdout, stderr, status = Open3.capture3("kubectl", "kustomize", path)
  raise "kubectl kustomize #{path} failed:\n#{stderr}" unless status.success?

  YAML.load_stream(stdout).compact
end

def network_policy(documents, name)
  documents.select do |document|
    document["apiVersion"] == "networking.k8s.io/v1" &&
      document["kind"] == "NetworkPolicy" &&
      document.dig("metadata", "name") == name
  end
end

errors = []
app_directories = Dir.children(APPS_ROOT).sort.each_with_object([]) do |entry, directories|
  path = File.join(APPS_ROOT, entry)
  directories << path if !entry.start_with?("_") && File.file?(File.join(path, "kustomization.yaml"))
end

expected_dns_spec = nil

begin
  shared_baseline = render(File.join(APPS_ROOT, "_shared/network-policy/baseline"))
  baseline = network_policy(shared_baseline, "default-deny-all")
  unless baseline.length == 1 && baseline.first["spec"] == EXPECTED_BASELINE_SPEC
    errors << "shared baseline must render exactly one ingress-and-egress default-deny-all policy"
  end
rescue StandardError => error
  errors << error.message
end

begin
  shared_dns = render(File.join(APPS_ROOT, "_shared/network-policy/dns-egress"))
  dns = network_policy(shared_dns, "allow-dns-egress")
  if dns.length != 1
    errors << "shared DNS profile must render exactly one allow-dns-egress policy"
  else
    expected_dns_spec = dns.first["spec"]
  end
rescue StandardError => error
  errors << error.message
end

app_directories.each do |directory|
  app = File.basename(directory)
  kustomization_path = File.join(directory, "kustomization.yaml")
  namespace_path = File.join(directory, "namespace.yaml")
  kustomization = YAML.load_file(kustomization_path)
  resources = Array(kustomization["resources"])
  expected_namespace = YAML.load_file(namespace_path).dig("metadata", "name")
  requires_baseline = !BASELINE_EXCEPTIONS.include?(app)
  uses_shared_dns = SHARED_DNS_APPS.include?(app)

  if resources.include?(BASELINE_RESOURCE) != requires_baseline
    errors << "#{app}: baseline reference must be #{requires_baseline ? "present" : "absent"}"
  end
  if resources.include?(DNS_RESOURCE) != uses_shared_dns
    errors << "#{app}: shared DNS reference must be #{uses_shared_dns ? "present" : "absent"}"
  end
  if requires_baseline && kustomization["namespace"] != expected_namespace
    errors << "#{app}: kustomization namespace must be #{expected_namespace.inspect}"
  end

  begin
    documents = render(directory)
  rescue StandardError => error
    errors << error.message
    next
  end

  identities = documents.map do |document|
    [document["apiVersion"], document["kind"], document.dig("metadata", "namespace"), document.dig("metadata", "name")]
  end
  duplicates = identities.group_by(&:itself).select { |_identity, entries| entries.length > 1 }.keys
  errors << "#{app}: duplicate rendered resource identities: #{duplicates.inspect}" unless duplicates.empty?

  baseline = network_policy(documents, "default-deny-all")
  expected_count = requires_baseline ? 1 : 0
  if baseline.length != expected_count
    errors << "#{app}: expected #{expected_count} default-deny-all policy, rendered #{baseline.length}"
  elsif requires_baseline
    policy = baseline.first
    errors << "#{app}: default-deny-all rendered in #{policy.dig("metadata", "namespace").inspect}" unless policy.dig("metadata", "namespace") == expected_namespace
    errors << "#{app}: default-deny-all spec changed" unless policy["spec"] == EXPECTED_BASELINE_SPEC
  end

  next unless uses_shared_dns

  dns = network_policy(documents, "allow-dns-egress")
  if dns.length != 1
    errors << "#{app}: expected one shared allow-dns-egress policy, rendered #{dns.length}"
  else
    policy = dns.first
    errors << "#{app}: allow-dns-egress rendered in #{policy.dig("metadata", "namespace").inspect}" unless policy.dig("metadata", "namespace") == expected_namespace
    errors << "#{app}: shared allow-dns-egress spec changed" unless policy["spec"] == expected_dns_spec
  end
end

if errors.empty?
  puts "validated #{app_directories.length} app kustomizations and shared network-policy invariants"
  exit 0
end

warn errors.map { |error| "- #{error}" }.join("\n")
exit 1
