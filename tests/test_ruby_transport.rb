require 'minitest/autorun'
require 'json'
require 'socket'
require 'tmpdir'
require 'timeout'

# 只替代宿主 UI；网络、队列、线程及被测 Server 均使用真实实现。
module UI
  class << self
    attr_accessor :console_hook

    def start_timer(_seconds, _repeat, &block)
      raise 'UI called from worker' unless Thread.current == Thread.main
      @next_id = (@next_id || 0) + 1
      timers[@next_id] = block
      @next_id
    end

    def stop_timer(id)
      raise 'UI called from worker' unless Thread.current == Thread.main
      timers.delete(id)
    end

    def timers
      @timers ||= {}
    end
  end
end

SKETCHUP_CONSOLE = Object.new
def SKETCHUP_CONSOLE.show; end
def SKETCHUP_CONSOLE.write(_message)
  raise 'Console called from worker' unless Thread.current == Thread.main
  UI.console_hook.call if UI.console_hook
end

module Sketchup
  def self.read_default(_namespace, _key, default); default; end
end

$LOADED_FEATURES << 'sketchup.rb'
def file_loaded?(_path); true; end
require_relative '../su_mcp/su_mcp/main'

class TransportHarness < SU_MCP::Server
  attr_accessor :dispatch_hook
  attr_reader :dispatch_threads, :read_threads, :write_threads

  def initialize
    super
    @dispatch_threads = []
    @read_threads = []
    @write_threads = []
  end

  def initialize_eval_ruby_log; end
  def write_instance_registry; end
  def remove_instance_registry; end

  def handle_jsonrpc_request(request)
    @dispatch_threads << Thread.current
    return dispatch_hook.call(request) if dispatch_hook
    super
  end

  def read_client_payload(client)
    @read_threads << Thread.current
    super
  end

  def write_json_response(client, response)
    @write_threads << Thread.current
    super
  end
end

class RubyTransportTest < Minitest::Test
  def setup
    @server = TransportHarness.new
    @sockets = []
    @transports = []
    start_server
  end

  def teardown
    UI.console_hook = nil
    @server.stop
    @sockets.each { |socket| socket.close unless socket.closed? }
    @transports.each do |transport|
      transport[:workers].each { |thread| assert thread.join(2), 'I/O worker failed to stop' }
    end
    assert_empty UI.timers
  end

  def start_server
    socket = TCPServer.new('127.0.0.1', 0)
    @port = socket.addr[1]
    assert @server.start(socket)
    @transport = @server.instance_variable_get(:@transport)
    @transports << @transport
    @tick = UI.timers.values.last
  end

  def connect
    socket = TCPSocket.new('127.0.0.1', @port)
    @sockets << socket
    socket
  end

  def tool(name = 'ping', id = 'request-中文', arguments = {})
    { 'jsonrpc' => '2.0', 'method' => 'tools/call',
      'params' => { 'name' => name, 'arguments' => arguments }, 'id' => id }
  end

  def frame(request)
    body = JSON.generate(request)
    "Content-Length: #{body.bytesize}\r\n\r\n".b + body.b
  end

  def wait_until
    Timeout.timeout(3) do
      until yield
        sleep 0.002
      end
    end
  end

  def wait_for_requests(count = 1, transport = @transport)
    wait_until { transport[:requests].size >= count }
  end

  def receive_response(socket)
    Timeout.timeout(3) do
      header = socket.gets
      raise 'Missing Content-Length' unless header && header.start_with?('Content-Length: ')
      length = header.split(':', 2).last.to_i
      assert_equal "\r\n", socket.gets
      JSON.parse(socket.read(length))
    end
  end

  def enqueue(request, transport = @transport)
    reply = Queue.new
    transport[:requests] << [request, reply]
    reply
  end

  def test_framed_split_utf8_request_and_response_keep_string_id
    socket = connect
    payload = frame(tool)
    payload.bytes.each_slice(5) { |slice| socket.write(slice.pack('C*')) }
    wait_for_requests
    @tick.call
    response = receive_response(socket)
    assert_equal 'request-中文', response['id']
    assert_equal 'pong', response.dig('result', 'value')
    assert_equal [Thread.main], @server.dispatch_threads
    assert @server.read_threads.all? { |thread| thread != Thread.main }
    assert @server.write_threads.all? { |thread| thread != Thread.main }
  end

  def test_legacy_line_json_and_direct_command_still_work
    socket = connect
    socket.write(JSON.generate('command' => 'ping', 'parameters' => {}, 'id' => 42) + "\n")
    wait_for_requests
    @tick.call
    response = receive_response(socket)
    assert_equal 42, response['id']
    assert_equal 'pong', response.dig('result', 'value')
  end

  def test_console_reentry_is_rejected_before_dispatch
    events = []
    first = enqueue(tool('ping', 1))
    second = enqueue(tool('ping', 2))
    @transport[:logs] << 'worker log'
    UI.console_hook = lambda do
      @tick.call
      @tick.call
      events << 'console'
    end
    @server.dispatch_hook = ->(request) { events << request['id']; { id: request['id'] } }
    @tick.call
    assert_equal ['console', 1], events
    assert_equal({ id: 1 }, first.pop(true))
    assert second.empty?
    UI.console_hook = nil
    @tick.call
    assert_equal({ id: 2 }, second.pop(true))
  end

  def test_eval_reentry_does_not_nest_requests_or_unlock_outer_callback
    events = []
    first = enqueue(tool('ping', 1))
    second = enqueue(tool('ping', 2))
    @server.dispatch_hook = lambda do |request|
      events << "#{request['id']}.begin"
      @tick.call
      @tick.call
      events << "#{request['id']}.end"
      { id: request['id'] }
    end
    @tick.call
    assert_equal ['1.begin', '1.end'], events
    assert_equal({ id: 1 }, first.pop(true))
    assert second.empty?
    @tick.call
    assert_equal ['1.begin', '1.end', '2.begin', '2.end'], events
  end

  def test_real_eval_keeps_structured_result_and_defers_nested_timer
    $transport_test_events = []
    $transport_test_tick = @tick
    code = "$transport_test_events << 'A.begin'; $transport_test_tick.call; " \
           "$transport_test_events << 'A.end'; { 'items' => [1, true, nil] }"
    first = enqueue(tool('eval_ruby', 1, 'code' => code))
    second = enqueue(tool('eval_ruby', 2, 'code' => "$transport_test_events << 'B'"))
    @tick.call
    assert_equal ['A.begin', 'A.end'], $transport_test_events
    assert_equal({ 'items' => [1, true, nil] }, first.pop(true).dig(:result, :value))
    assert second.empty?
    @tick.call
    assert_equal ['A.begin', 'A.end', 'B'], $transport_test_events
  ensure
    $transport_test_events = nil
    $transport_test_tick = nil
  end

  def test_dispatch_error_releases_guard_and_replies_to_waiter
    first = enqueue(tool('ping', 1))
    @server.dispatch_hook = ->(_request) { raise SyntaxError, 'broken script' }
    @tick.call
    assert_equal 'broken script', first.pop(true).dig(:error, :message)
    second = enqueue(tool('ping', 2))
    @server.dispatch_hook = nil
    @tick.call
    assert_equal 'pong', second.pop(true).dig(:result, :value)
  end

  def test_expired_queued_write_is_not_executed
    request = tool('eval_ruby', 1, 'code' => "raise 'must not execute'")
    request['_mcp'] = { 'sent_at_ms' => (Time.now.to_f * 1000).to_i, 'timeout_ms' => 1 }
    reply = enqueue(request)
    sleep 0.01
    @tick.call
    response = reply.pop(true)
    assert_equal(-32001, response.dig(:error, :code))
    assert_equal 'not_started', response.dig(:error, :data, :execution_state)
    assert_empty @server.dispatch_threads
  end

  def test_silent_and_partial_clients_do_not_block_main_thread_or_other_clients
    silent = connect
    partial = connect
    partial.write("Content-Length: 20\r\n\r\n{")
    wait_until { @server.read_threads.size >= 2 }
    socket = connect
    socket.write(frame(tool))
    wait_for_requests
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    @tick.call
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
    assert_operator elapsed, :<, 0.2
    assert_equal 'pong', receive_response(socket).dig('result', 'value')
    wait_until { @transport[:mutex].synchronize { @transport[:connections].empty? } }
    assert_nil silent.read(1)
    assert_nil partial.read(1)
  end

  def test_invalid_json_does_not_kill_worker_or_require_main_tick
    socket = connect
    socket.write("{broken\n")
    assert_equal(-32700, receive_response(socket).dig('error', 'code'))
    assert_empty @server.dispatch_threads
    assert_equal SU_MCP::Server::IO_WORKER_COUNT, @transport[:workers].count(&:alive?)
  end

  def test_read_error_does_not_permanently_remove_a_pool_worker
    original_reader = @server.method(:read_client_payload)
    failed = Queue.new
    @server.define_singleton_method(:read_client_payload) do |client|
      if failed.empty?
        failed << true
        raise ArgumentError, 'invalid incoming encoding'
      end
      original_reader.call(client)
    end
    socket = connect
    socket.write("invalid\n")
    wait_until { !failed.empty? && @transport[:mutex].synchronize { @transport[:connections].empty? } }
    assert_equal SU_MCP::Server::IO_WORKER_COUNT, @transport[:workers].count(&:alive?)
    socket = connect
    socket.write(frame(tool))
    wait_for_requests
    @tick.call
    assert_equal 'pong', receive_response(socket).dig('result', 'value')
  end

  def test_stop_wakes_pending_reply_and_readers_without_killing_threads
    connect
    socket = connect
    socket.write(frame(tool))
    wait_for_requests
    transport = @transport
    @server.stop
    transport[:workers].each { |thread| assert thread.join(2) }
    assert transport[:mutex].synchronize { transport[:connections].empty? }
    assert_nil socket.read(1)
    @tick.call
    assert_empty @server.dispatch_threads
  end

  def test_old_timer_and_queue_cannot_execute_after_restart
    old_tick = @tick
    old_reply = enqueue(tool('ping', 1))
    @server.stop
    start_server
    new_reply = enqueue(tool('ping', 2))
    old_tick.call
    assert_empty @server.dispatch_threads
    assert old_reply.empty?
    assert new_reply.empty?
    @tick.call
    assert_equal 2, new_reply.pop(true)[:id]
  end

  def test_timer_creation_failure_cleans_up_new_transport
    @server.stop
    socket = TCPServer.new('127.0.0.1', 0)
    failed_transport = nil
    UI.stub(:start_timer, lambda { |*_args|
      failed_transport = @server.instance_variable_get(:@transport)
      raise 'timer unavailable'
    }) do
      refute @server.start(socket)
    end
    @transports << failed_transport
    assert socket.closed?
    assert_nil @server.instance_variable_get(:@transport)
    refute @server.instance_variable_get(:@starting)
    start_server
    reply = enqueue(tool)
    @tick.call
    assert_equal 'pong', reply.pop(true).dig(:result, :value)
  end

  def test_stop_reentry_cannot_start_a_new_listener_mid_cleanup
    candidate = TCPServer.new('127.0.0.1', 0)
    @sockets << candidate
    attempted = false
    start_result = nil
    UI.console_hook = lambda do
      unless attempted
        attempted = true
        start_result = @server.start(candidate)
        nested_transport = @server.instance_variable_get(:@transport)
        @transports << nested_transport if nested_transport
        @server.stop
      end
    end
    @server.stop
    assert attempted
    refute start_result, 'start must be rejected until the outer stop finishes'
    assert_nil @server.instance_variable_get(:@transport)
    refute @server.instance_variable_get(:@running)
    assert_empty UI.timers
  end

  def test_stop_requested_during_start_is_honored_before_return
    @server.stop
    candidate = TCPServer.new('127.0.0.1', 0)
    @sockets << candidate
    requested = false
    UI.console_hook = lambda do
      unless requested
        requested = true
        @server.stop
      end
    end
    result = @server.start(candidate)
    transport = @server.instance_variable_get(:@transport)
    @transports << transport if transport
    refute result, 'a reentrant stop request must not be overwritten by start'
    assert_nil transport
    assert candidate.closed?
    assert_empty UI.timers
    refute @server.instance_variable_get(:@starting)
  end

  def test_lifecycle_calls_from_worker_fail_before_mutating_state
    errors = Queue.new
    worker = Thread.new do
      begin
        @server.stop
      rescue StandardError => error
        errors << error
      end
    end
    assert worker.join(2)
    assert_instance_of ThreadError, errors.pop(true)
    assert_same @transport, @server.instance_variable_get(:@transport)
    assert @transport[:running]
    reply = enqueue(tool)
    @tick.call
    assert_equal 'pong', reply.pop(true).dig(:result, :value)
  end

  def test_timer_stop_error_does_not_skip_remaining_cleanup
    old_tick = @tick
    old_timer_id = UI.timers.keys.last
    old_transport = @transport
    removed = false
    @server.define_singleton_method(:remove_instance_registry) { removed = true }
    UI.stub(:stop_timer, ->(_id) { raise 'host timer failure' }) { @server.stop }
    assert removed
    refute @server.instance_variable_get(:@stopping)
    assert old_transport[:server].closed?
    old_transport[:workers].each { |thread| assert thread.join(2) }
    start_server
    reply = enqueue(tool)
    old_tick.call
    assert reply.empty?, 'an old native timer must remain inert even if cancellation failed'
    @tick.call
    assert_equal 'pong', reply.pop(true).dig(:result, :value)
  ensure
    UI.timers.delete(old_timer_id)
  end

  def test_restart_during_dispatch_does_not_allow_new_timer_to_reenter
    events = []
    old_reply = enqueue(tool('ping', 1))
    new_reply = nil
    @server.dispatch_hook = lambda do |request|
      events << request['id']
      if request['id'] == 1
        @server.stop
        start_server
        new_reply = enqueue(tool('ping', 2))
        @tick.call
        assert new_reply.empty?
      end
      { id: request['id'] }
    end
    @tick.call
    assert_equal [1], events
    assert old_reply.empty?
    @tick.call
    assert_equal [1, 2], events
    assert_equal({ id: 2 }, new_reply.pop(true))
  end

  def test_workers_and_log_queue_are_bounded
    (SU_MCP::Server::IO_WORKER_COUNT + 3).times { connect }
    wait_until { @transport[:mutex].synchronize { @transport[:connections].size == SU_MCP::Server::IO_WORKER_COUNT } }
    assert_equal SU_MCP::Server::IO_WORKER_COUNT, @transport[:workers].count(&:alive?)
    transport = @transport
    worker = Thread.new do
      Thread.current[:su_mcp_transport] = transport
      (SU_MCP::Server::MAX_QUEUED_LOGS + 10).times { @server.log('background') }
    end
    assert worker.join(1)
    assert_equal SU_MCP::Server::MAX_QUEUED_LOGS, transport[:logs].size
    @tick.call
    assert_equal SU_MCP::Server::MAX_QUEUED_LOGS - SU_MCP::Server::MAX_LOGS_PER_TICK, transport[:logs].size
  end

  def test_backpressured_response_does_not_block_tick
    socket = connect
    # 客户端不读取大响应时，主线程仍能处理下一次回调。
    @server.dispatch_hook = ->(_request) { { id: 1, result: 'x' * (16 * 1024 * 1024) } }
    socket.write(frame(tool))
    wait_for_requests
    clock = Process.method(:clock_gettime)
    @tick.call
    wait_until { !@server.write_threads.empty? }
    started = clock.call(Process::CLOCK_MONOTONIC)
    @tick.call
    assert_operator clock.call(Process::CLOCK_MONOTONIC) - started, :<, 0.2
    socket.close
    wait_until { @transport[:mutex].synchronize { @transport[:connections].empty? } }
    assert_equal SU_MCP::Server::IO_WORKER_COUNT, @transport[:workers].count(&:alive?)
  end

  def test_partial_writes_and_write_timeout_preserve_frame
    fake_socket = Object.new
    written = +''.b
    fake_socket.define_singleton_method(:write_nonblock) do |data|
      size = [3, data.bytesize].min
      written << data.byteslice(0, size)
      size
    end
    response = { id: '中文', result: 'pong' }
    IO.stub(:select, ->(*_args) { [[], [fake_socket], []] }) do
      @server.send(:write_json_response, fake_socket, response)
    end
    assert_equal frame(response), written
    IO.stub(:select, nil) do
      error = assert_raises(IOError) { @server.send(:write_json_response, fake_socket, response) }
      assert_match(/write timed out/, error.message)
    end
  end
end
