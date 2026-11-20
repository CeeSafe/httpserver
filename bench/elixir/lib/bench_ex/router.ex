defmodule BenchEx.Router do
  @moduledoc """
  The routes of httpserver's bench/src/main.cee that tools/elixir_bench.py measures, with the same bytes:

      GET  /small            "hello, world\\n"
      GET  /4k, /64k         that many bytes of the alphabet: byte i is ?a + rem(i, 26)
      GET  /stream           1 MiB: a 16 KiB piece of that alphabet written 64 times, with no declared length
      GET  /json             {"message":"Hello, World!"}, serialised for each request
      POST /echo             the request body back (at most 1 MiB)
      GET  /crash            raises: Bandit answers 500, as Plug's error handling does for any exception
      GET  /crash/after/:n   sends the head and n bytes (at most 64 KiB) of a chunked body, then raises
      GET  /spin/:ms         computes for ms milliseconds without waiting (at most a minute), then answers "spun"
      GET  /spin             computes without end
  """
  use Plug.Router

  plug :match
  plug :dispatch

  @alphabet for i <- 0..65535, into: <<>>, do: <<?a + rem(i, 26)>>
  @k4 binary_part(@alphabet, 0, 4096)
  @k64 @alphabet
  @piece binary_part(@alphabet, 0, 16384)

  get "/small" do
    send_resp(conn, 200, "hello, world\n")
  end

  get "/4k" do
    send_resp(conn, 200, @k4)
  end

  get "/64k" do
    send_resp(conn, 200, @k64)
  end

  get "/stream" do
    conn = send_chunked(conn, 200)
    Enum.reduce(1..64, conn, fn _, c -> elem(chunk(c, @piece), 1) end)
  end

  get "/json" do
    body = Jason.encode_to_iodata!(%{message: "Hello, World!"})

    conn
    |> put_resp_header("content-type", "application/json")
    |> send_resp(200, body)
  end

  post "/echo" do
    {body, conn} = read_all(conn, [])
    send_resp(conn, 200, body)
  end

  get "/crash" do
    # Out of bounds, as the CeeSafe handler's index is: an ArgumentError.
    _ = elem({1, 2, 3}, byte_size(conn.request_path) + 100)
    send_resp(conn, 200, "")
  end

  get "/crash/after/:n" do
    n = n |> String.to_integer() |> max(1) |> min(65536)
    conn = send_chunked(conn, 200)
    {:ok, conn} = chunk(conn, :binary.copy("x", n))
    _ = elem({1, 2, 3}, byte_size(conn.request_path) + 100)
    conn
  end

  get "/spin/:ms" do
    ms = ms |> String.to_integer() |> max(0) |> min(60_000)
    deadline = System.monotonic_time(:millisecond) + ms
    _ = spin_until(byte_size(conn.request_path), deadline)
    send_resp(conn, 200, "spun")
  end

  get "/spin" do
    _ = spin_forever(byte_size(conn.request_path))
    send_resp(conn, 200, "")
  end

  match _ do
    send_resp(conn, 404, "")
  end

  defp read_all(conn, acc) do
    case read_body(conn, length: 1_048_576) do
      {:ok, last, conn} ->
        body = IO.iodata_to_binary([acc, last])
        {binary_part(body, 0, min(byte_size(body), 1_048_576)), conn}

      {:more, part, conn} -> read_all(conn, [acc, part])
    end
  end

  # As the CeeSafe handlers do: read the clock until the deadline, and for /spin, without end.
  defp spin_until(n, deadline) do
    if System.monotonic_time(:millisecond) >= deadline, do: n, else: spin_until(n + 1, deadline)
  end

  defp spin_forever(n), do: spin_forever(n + System.monotonic_time() - System.monotonic_time())
end
