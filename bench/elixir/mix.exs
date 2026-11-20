defmodule BenchEx.MixProject do
  use Mix.Project

  # The Elixir side of tools/elixir_bench.py: Bandit and Plug, no Phoenix, answering the routes of bench/src/main.cee
  # that the comparison measures. `mix.lock` pins every package.
  def project do
    [
      app: :bench_ex,
      version: "0.1.0",
      elixir: "~> 1.18",
      start_permanent: true,
      deps: deps(),
      releases: [bench_ex: [include_executables_for: [:unix]]]
    ]
  end

  def application do
    [extra_applications: [:logger], mod: {BenchEx.Application, []}]
  end

  defp deps do
    [
      {:bandit, "~> 1.6"},
      {:plug, "~> 1.16"},
      {:jason, "~> 1.4"}
    ]
  end
end
