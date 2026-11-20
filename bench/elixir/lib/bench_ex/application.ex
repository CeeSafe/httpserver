defmodule BenchEx.Application do
  @moduledoc false
  use Application

  # Bandit on 127.0.0.1:CW_PORT (8090 by default), HTTP/1.1 and h2c with prior knowledge, with its defaults
  # otherwise: a process per connection (HTTP/1.1) or per stream (HTTP/2), and the schedulers the VM starts, one per
  # CPU it may run on (tools/elixir_bench.py sets +S to the CPUs it pins the server to).
  @impl true
  def start(_type, _args) do
    port = String.to_integer(System.get_env("CW_PORT", "8090"))

    children = [
      {Bandit, plug: BenchEx.Router, scheme: :http, ip: {127, 0, 0, 1}, port: port}
    ]

    Supervisor.start_link(children, strategy: :one_for_one, name: BenchEx.Supervisor)
  end
end
