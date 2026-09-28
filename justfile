# List available recipes
default:
    @just --list

# Run the test suite (extra args go to pytest, e.g. `just test -k unsupported`)
test *args:
    uv run pytest {{args}}

# Serve a folder as a virtual shell over MCP (stdio), e.g. `just serve ./src --read-only`
serve root="." *args:
    uv run mcpath {{root}} {{args}}
