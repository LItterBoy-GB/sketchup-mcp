require "minitest/autorun"
require "fileutils"
require "tmpdir"

RUBY_MAIN = File.expand_path("../su_mcp/su_mcp/main.rb", __dir__)

MB_OK = 0 unless Object.const_defined?(:MB_OK, false)
MB_OKCANCEL = 1 unless Object.const_defined?(:MB_OKCANCEL, false)
MB_ABORTRETRYIGNORE = 2 unless Object.const_defined?(:MB_ABORTRETRYIGNORE, false)
MB_YESNOCANCEL = 3 unless Object.const_defined?(:MB_YESNOCANCEL, false)
MB_YESNO = 4 unless Object.const_defined?(:MB_YESNO, false)
MB_RETRYCANCEL = 5 unless Object.const_defined?(:MB_RETRYCANCEL, false)
MB_ICONQUESTION = 0x20 unless Object.const_defined?(:MB_ICONQUESTION, false)

IDOK = 10 unless Object.const_defined?(:IDOK, false)
IDCANCEL = 11 unless Object.const_defined?(:IDCANCEL, false)
IDABORT = 12 unless Object.const_defined?(:IDABORT, false)
IDYES = 14 unless Object.const_defined?(:IDYES, false)
IDNO = 13 unless Object.const_defined?(:IDNO, false)

module UI
  MB_OK = ::MB_OK unless const_defined?(:MB_OK, false)
  MB_OKCANCEL = ::MB_OKCANCEL unless const_defined?(:MB_OKCANCEL, false)
  MB_ABORTRETRYIGNORE = ::MB_ABORTRETRYIGNORE unless const_defined?(:MB_ABORTRETRYIGNORE, false)
  MB_YESNOCANCEL = ::MB_YESNOCANCEL unless const_defined?(:MB_YESNOCANCEL, false)
  MB_YESNO = ::MB_YESNO unless const_defined?(:MB_YESNO, false)
  MB_RETRYCANCEL = ::MB_RETRYCANCEL unless const_defined?(:MB_RETRYCANCEL, false)
  MB_ICONQUESTION = ::MB_ICONQUESTION unless const_defined?(:MB_ICONQUESTION, false)

  IDOK = ::IDOK unless const_defined?(:IDOK, false)
  IDCANCEL = ::IDCANCEL unless const_defined?(:IDCANCEL, false)
  IDABORT = ::IDABORT unless const_defined?(:IDABORT, false)
  IDYES = ::IDYES unless const_defined?(:IDYES, false)
  IDNO = ::IDNO unless const_defined?(:IDNO, false)
end

def extract_method_source(source, method_name, required: true)
  lines = source.lines
  start_index = lines.index { |line| line.match?(/^\s*def #{Regexp.escape(method_name)}(?=\s|\(|$)/) }
  raise "method #{method_name} not found" if required && !start_index
  return nil unless start_index

  depth = 0
  lines[start_index..].each_with_index do |line, offset|
    stripped = line.strip
    depth += 1 if stripped.match?(/\A(def|begin|case|class|module|if|unless|while|until|for)\b/) || stripped.match?(/\bdo\b/)
    depth -= 1 if stripped.match?(/\Aend\b/)

    return lines[start_index, offset + 1].join if depth.zero?
  end

  raise "method #{method_name} end not found"
end

class EvalRubyLoggingTest < Minitest::Test
  class Harness
    EVAL_RUBY_UI_GUARD_METHODS = [:messagebox, :openpanel, :savepanel, :inputbox].freeze
    EVAL_RUBY_UI_FALLBACK_CONSTANTS = {
      MB_OK: 0,
      MB_OKCANCEL: 1,
      MB_ABORTRETRYIGNORE: 2,
      MB_YESNOCANCEL: 3,
      MB_YESNO: 4,
      MB_RETRYCANCEL: 5,
      IDOK: 1,
      IDCANCEL: 2,
      IDABORT: 3,
      IDRETRY: 4,
      IDIGNORE: 5,
      IDYES: 6,
      IDNO: 7
    }.freeze

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
    eval_ruby_ui_guard_enabled?
    with_eval_ruby_ui_guard
    eval_ruby_messagebox_result
    eval_ruby_ui_constant
    append_eval_ruby_ui_event
    summarize_eval_ruby_ui_arg
    eval_ruby_ui_result_label
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

  def test_prevent_modal_hang_defaults_to_existing_ui_behavior
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      UI.define_singleton_method(:openpanel) { |_title| raise "original openpanel" }

      error = assert_raises(RuntimeError) do
        harness.eval_ruby("code" => "UI.openpanel('Pick file')")
      end

      assert_equal "Ruby evaluation error: original openpanel", error.message
    end
  ensure
    UI.singleton_class.remove_method(:openpanel) if UI.respond_to?(:openpanel)
  end

  def test_prevent_modal_hang_returns_nil_for_file_and_input_ui
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      code = <<~RUBY
        [
          UI.openpanel('Open', '.', '*.skp').inspect,
          UI.savepanel('Save', '.', 'out.skp').inspect,
          UI.inputbox(['Name'], ['A'], 'Title').inspect
        ].join(',')
      RUBY

      result = harness.eval_ruby("code" => code, "prevent_modal_hang" => true)

      assert_equal true, result[:success]
      assert_equal "nil,nil,nil", result[:result]
      assert_equal ["UI.openpanel", "UI.savepanel", "UI.inputbox"], result[:ui_events].map { |event| event[:api] }
    end
  end

  def test_prevent_modal_hang_uses_safe_messagebox_results
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      code = <<~RUBY
        [
          UI.messagebox('ok', MB_OK),
          UI.messagebox('cancel', MB_OKCANCEL),
          UI.messagebox('no', MB_YESNO),
          UI.messagebox('cancel wins', MB_YESNOCANCEL),
          UI.messagebox('abort', MB_ABORTRETRYIGNORE)
        ].join(',')
      RUBY

      result = harness.eval_ruby("code" => code, "prevent_modal_hang" => true)

      assert_equal true, result[:success]
      assert_equal [IDOK, IDCANCEL, IDNO, IDCANCEL, IDABORT].join(","), result[:result]
      assert_equal 5, result[:ui_events].length
    end
  end

  def test_prevent_modal_hang_handles_messagebox_style_flags
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      code = <<~RUBY
        [
          UI.messagebox('ok with icon', MB_OK | MB_ICONQUESTION),
          UI.messagebox('no with icon', MB_YESNO | MB_ICONQUESTION)
        ].join(',')
      RUBY

      result = harness.eval_ruby("code" => code, "prevent_modal_hang" => true)

      assert_equal true, result[:success]
      assert_equal [IDOK, IDNO].join(","), result[:result]
    end
  end

  def test_prevent_modal_hang_restores_ui_methods_after_error
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)
      UI.define_singleton_method(:openpanel) { |_title| "original" }
      original_method = UI.method(:openpanel)

      error = assert_raises(RuntimeError) do
        harness.eval_ruby(
          "code" => "UI.openpanel('Pick file'); raise 'boom'",
          "prevent_modal_hang" => true
        )
      end

      assert_equal "Ruby evaluation error: boom", error.message
      assert_equal original_method, UI.method(:openpanel)
      assert_equal "original", UI.openpanel("Pick file")
    end
  ensure
    UI.singleton_class.remove_method(:openpanel) if UI.respond_to?(:openpanel)
  end

  def test_prevent_modal_hang_logs_ui_events_to_session_file
    Dir.mktmpdir do |dir|
      harness = Harness.new(dir)

      result = harness.eval_ruby(
        "code" => "UI.inputbox(['Name'], ['A'], 'Title'); 'done'",
        "prevent_modal_hang" => true
      )

      assert_equal true, result[:success]
      assert_equal "done", result[:result]

      log_file = Dir.children(dir).grep(/\.rb\z/).map { |name| File.join(dir, name) }.first
      content = File.read(log_file, encoding: "UTF-8")
      assert_includes content, "# ui_event_api: UI.inputbox"
      assert_includes content, "# ui_event_result: nil"
    end
  end
end
