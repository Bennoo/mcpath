# List available recipes
default:
    @just --list

# Run the test suite (extra args go to pytest, e.g. `just test -k unsupported`)
test *args:
    uv run pytest {{args}}
