require "minitest/autorun"
require "socket"

MAIN_RB_PATH = File.expand_path("../su_mcp/su_mcp/main.rb", __dir__)

def extract_method_source(source, method_name)
  lines = source.lines
  start_index = lines.index { |line| line.match?(/^\s*def #{Regexp.escape(method_name)}\b/) }
  raise "Method not found: #{method_name}" unless start_index

  depth = 0
  lines[start_index..].each_with_index do |line, offset|
    stripped = line.strip
    depth += 1 if stripped.match?(/\A(def|begin|case|class|module|if|unless|while|until|for)\b/) || stripped.match?(/\bdo\b/)
    depth -= 1 if stripped.match?(/\Aend\b/)
    return lines[start_index, offset + 1].join if depth.zero?
  end

  raise "Unterminated method: #{method_name}"
end

module Sketchup
  class << self
    attr_accessor :writes

    def write_default(namespace, key, value)
      self.writes ||= []
      writes << [namespace, key, value]
      true
    end
  end
end

class AutoStartHarness
  DEFAULT_PORT = 9876
  SETTINGS_NAMESPACE = "SU_MCP"
  SETTINGS_PORT_KEY = "port"

  attr_reader :port, :logs, :menu_updates, :started_socket

  def self.valid_port?(port)
    port.is_a?(Integer) && port >= 1 && port <= 65_535
  end

  def initialize
    @port = 12_345
    @running = false
    @logs = []
    @menu_updates = 0
  end

  def log(message)
    @logs << message
  end

  def update_port_menu_text
    @menu_updates += 1
  end

  def start(server_socket = nil)
    @started_socket = server_socket
    @running = true
  end
end

source = File.read(MAIN_RB_PATH, encoding: "UTF-8")
%w[parse_port bind_available_server start_on_available_port].each do |method_name|
  AutoStartHarness.class_eval(extract_method_source(source, method_name), MAIN_RB_PATH)
end

class AutoStartTest < Minitest::Test
  def setup
    Sketchup.writes = []
  end

  def test_tries_successive_ports_and_starts_with_the_bound_socket
    attempts = []
    bound_socket = Object.new
    tcp_server = lambda do |_host, port|
      attempts << port
      raise Errno::EADDRINUSE if port < 9878

      bound_socket
    end

    server = AutoStartHarness.new
    result = TCPServer.stub(:new, tcp_server) do
      server.start_on_available_port(9876)
    end

    assert result
    assert_equal [9876, 9877, 9878], attempts
    assert_equal 9878, server.port
    assert_same bound_socket, server.started_socket
    assert_equal [["SU_MCP", "port", 9878]], Sketchup.writes
    assert_equal 1, server.menu_updates
    assert_includes server.logs, "Automatically started MCP server on port 9878"
  end

  def test_keeps_the_previous_port_when_no_candidate_can_be_bound
    server = AutoStartHarness.new

    result = TCPServer.stub(:new, ->(_host, _port) { raise Errno::EACCES }) do
      server.start_on_available_port(65_535)
    end

    refute result
    assert_equal 12_345, server.port
    assert_empty Sketchup.writes
    assert_includes server.logs, "No available MCP port found from 65535 through 65535."
  end

  def test_extension_load_schedules_automatic_start
    source = File.read(MAIN_RB_PATH, encoding: "UTF-8")

    assert_includes source, "UI.start_timer(0, false) do"
    assert_includes source, "@server.start_on_available_port(Server::DEFAULT_PORT)"
  end
end
