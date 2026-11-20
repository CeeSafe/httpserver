import Config

# A crashed request's report is one line, not a stack trace for each: under the fault runs Logger would otherwise
# spend the schedulers on formatting. Errors are still logged; requests are not.
config :logger, level: :error
config :logger, :default_formatter, format: "$level $message\n", truncate: 256
