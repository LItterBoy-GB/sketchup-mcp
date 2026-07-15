require "json"
require "minitest/autorun"
require "tmpdir"

RUBY_MAIN = File.expand_path("../su_mcp/su_mcp/main.rb", __dir__)

module Sketchup
  class << self
    attr_accessor :test_model

    def active_model
      test_model
    end

    def version
      "22.0.354"
    end
  end
end

def extract_method_source(source, method_name)
  lines = source.lines
  start_index = lines.index { |line| line.match?(/^\s*def #{Regexp.escape(method_name)}(?=\s|\(|$)/) }
  raise "method #{method_name} not found" unless start_index

  depth = 0
  lines[start_index..].each_with_index do |line, offset|
    stripped = line.strip
    depth += 1 if stripped.match?(/\A(def|begin|case|class|module|if|unless|while|until|for)\b/) || stripped.match?(/\bdo\b/)
    depth -= 1 if stripped.match?(/\Aend\b/)
    return lines[start_index, offset + 1].join if depth.zero?
  end

  raise "method #{method_name} end not found"
end

class InstanceRegistryHarness
  INSTANCE_REGISTRY_SCHEMA_VERSION = 1
  INSTANCE_PROTOCOL_VERSION = 1

  def initialize(directory)
    @directory = directory
    @instance_id = "instance-test"
    @started_at_ms = 123_456
    @port = 9876
    @logs = []
  end

  attr_reader :logs

  def instance_registry_dir
    @directory
  end

  def log(message)
    @logs << message
  end
end

source = File.read(RUBY_MAIN, encoding: "UTF-8")
%w[instance_registry_path write_instance_registry remove_instance_registry get_instance_info].each do |method_name|
  InstanceRegistryHarness.class_eval(extract_method_source(source, method_name))
end

class InstanceRegistryTest < Minitest::Test
  FakePage = Struct.new(:name)
  FakePages = Struct.new(:selected_page)
  FakeModel = Struct.new(:guid, :title, :path, :pages)

  def test_registry_writes_and_removes_its_own_entry
    Dir.mktmpdir do |directory|
      harness = InstanceRegistryHarness.new(directory)
      harness.send(:write_instance_registry)

      path = File.join(directory, "#{Process.pid}.json")
      payload = JSON.parse(File.read(path, encoding: "UTF-8"))
      assert_equal 9876, payload["port"]
      assert_equal Process.pid, payload["pid"]
      assert_equal "instance-test", payload["instance_id"]

      harness.send(:remove_instance_registry)
      refute File.exist?(path)
    end
  end

  def test_registry_does_not_delete_an_entry_owned_by_another_instance
    Dir.mktmpdir do |directory|
      harness = InstanceRegistryHarness.new(directory)
      harness.send(:write_instance_registry)
      path = File.join(directory, "#{Process.pid}.json")
      payload = JSON.parse(File.read(path, encoding: "UTF-8"))
      payload["instance_id"] = "other-instance"
      File.write(path, JSON.generate(payload), encoding: "UTF-8")

      harness.send(:remove_instance_registry)
      assert File.exist?(path)
    end
  end

  def test_instance_info_contains_model_fingerprint
    Sketchup.test_model = FakeModel.new("model-guid", "Test Model", "C:/tmp/test.skp", FakePages.new(FakePage.new("Page A")))
    payload = JSON.parse(InstanceRegistryHarness.new(Dir.tmpdir).send(:get_instance_info)[:result])

    assert_equal "instance-test", payload["instance_id"]
    assert_equal 9876, payload["port"]
    assert_equal "22.0.354", payload["sketchup_version"]
    assert_equal "C:/tmp/test.skp", payload["model_path"]
    assert_equal "Page A", payload["selected_page"]
  ensure
    Sketchup.test_model = nil
  end
end
