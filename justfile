# List available recipes
default:
    @just --list

# Run the test suite (extra args go to pytest, e.g. `just test -k unsupported`)
test *args:
    uv run pytest {{args}}

# Serve a folder as a virtual shell over MCP (stdio), e.g. `just serve ./src --read-only`
serve root="." *args:
    uv run mcpath {{root}} {{args}}

# Notes:
# - Binds all container interfaces: traffic from the port docker-compose.yml
#   publishes arrives on the container's network interface, not its loopback.
#   Safe because Docker only publishes it on the host's 127.0.0.1.
# - The Inspector's `--` is reversed: what comes BEFORE it goes to the server.

# Inspector web UI on a folder, at http://localhost:6274 (e.g. `just inspect ./src --read-only`)
inspect root="." *args:
    HOST=0.0.0.0 DANGEROUSLY_BIND_ALL_INTERFACES=true MCP_AUTO_OPEN_ENABLED=false MCP_INSPECTOR_SECRET_STORE=memory mcp-inspector uv run mcpath {{root}} {{args}} --

# Call the `run` tool once through the real MCP protocol, e.g. `just call ./src "ls | wc -l"`
call root command:
    MCP_INSPECTOR_SECRET_STORE=memory mcp-inspector --cli uv run mcpath {{root}} -- --method tools/call --tool-name run --tool-arg "command={{command}}"
