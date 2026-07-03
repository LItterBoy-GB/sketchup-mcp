require "minitest/autorun"
require "fileutils"
require "tmpdir"

RUBY_MAIN = File.expand_path("../su_mcp/su_mcp/main.rb", __dir__)

def extract_method_source(source, method_name, required: true)
  lines = source.lines
  start_index = lines.index { |line| line.match?(/^\s*def #{Regexp.escape(method_name)}\b/) }
  raise "method #{method_name} not found" if required && !start_index
  return nil unless start_index

  depth = 0
  lines[start_index..].each_with_index do |line, offset|
    stripped = line.strip
    depth += 1 if stripped.match?(/\A(def|begin|case|class|module|if|unless|while|until|for)\b/) || stripped.match?(/\bdo\b/)
    depth -= 1 if stripped == "end"

    return lines[start_index, offset + 1].join if depth.zero?
  end

  raise "method #{method_name} end not found"
end

class EvalRubyLoggingTest < Minitest::Test
  class Harness
    attr_reader :logs

    def initialize(log_dir)
      @logs = []
      @log_dir = log_dir
      initialize_eval_ruby_log if respond_to?(:initialize_eval_ruby_log, true)
    end

    def eval_ruby_log_dir
      @log_dir
    end

    def log(message)
      @logs << message
    end
  end

  source = File.read(RUBY_MAIN, encoding: "UTF-8")
  %w[
    initialize_eval_ruby_log
    append_eval_ruby_log_entry
    format_eval_ruby_log_comment
    eval_ruby
  ].each do |method_name|
    method_source = extract_method_source(source, method_name, required: method_name == "eval_ruby")
    Harness.class_eval(method_source) if method_source
  end

  def test_eval_ruby_uses_mcp_directory_for_session_log_files
    source = File.read(RUBY_MAIN, encoding: "UTF-8")

    assert_match(/def start.*initialize_eval_ruby_log.*TCPServer/m, source)
    assert_includes source, 'File.expand_path("../eval_ruby_logs", __dir__)'
    assert_match(/eval_ruby_#\{timestamp\}_#\{\$\$\}\.rb/, source)
  end

  def test_eval_ruby_appends_successful_code_to_session_rb_file
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      code = <<~RUBY
        value = 21
        value * 2
      RUBY

      result = harness.eval_ruby("code" => code)

      assert_equal true, result[:success]
      assert_equal "42", result[:result]

      log_files = Dir.children(dir).grep(/\.rb\z/).map { |name| File.join(dir, name) }
      assert_equal 1, log_files.length

      content = File.read(log_files.first, encoding: "UTF-8")
      assert_includes content, "# --- eval_ruby start ---"
      assert_includes content, "# status: success"
      assert_includes content, "# request_length: #{code.length}"
      assert_includes content, code
      assert_includes content, "# --- eval_ruby end ---"
    end
  end

  def test_eval_ruby_appends_failed_code_and_error_to_session_rb_file
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      code = <<~RUBY
        raise "boom"
      RUBY

      error = assert_raises(RuntimeError) do
        harness.eval_ruby("code" => code)
      end

      assert_equal "Ruby evaluation error: boom", error.message

      log_files = Dir.children(dir).grep(/\.rb\z/).map { |name| File.join(dir, name) }
      assert_equal 1, log_files.length

      content = File.read(log_files.first, encoding: "UTF-8")
      assert_includes content, "# status: error"
      assert_includes content, "# error_class: RuntimeError"
      assert_includes content, "# error: boom"
      assert_includes content, "# backtrace:"
      assert_includes content, code
    end
  end

  def test_multiple_eval_ruby_calls_append_to_same_session_file
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      harness.eval_ruby("code" => "1 + 1")
      harness.eval_ruby("code" => "2 + 2")

      log_files = Dir.children(dir).grep(/\.rb\z/).map { |name| File.join(dir, name) }
      assert_equal 1, log_files.length

      content = File.read(log_files.first, encoding: "UTF-8")
      assert_equal 2, content.scan("# --- eval_ruby start ---").length
      assert_includes content, "1 + 1"
      assert_includes content, "2 + 2"
    end
  end

  def test_eval_ruby_still_logs_brief_console_summary
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      code = <<~RUBY
        value = 21
        value * 2
      RUBY

      result = harness.eval_ruby("code" => code)

      assert_equal true, result[:success]
      assert_equal "42", result[:result]
      assert_includes harness.logs, "Evaluating Ruby code with length: #{code.length}"
      refute_includes harness.logs, "Ruby code:\n#{code}"
    end
  end
end