"""mcpath: an MCP server exposing a folder as a virtual, bash-like shell."""

import argparse

from .vfs import DEFAULT_DENY, VFS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcpath",
        description="Expose a folder (or any fsspec URL) to agents as a virtual shell over MCP.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="folder or fsspec URL mounted at / (default: current directory), "
        "e.g. ./project, memory://scratch, s3://bucket/prefix",
    )
    parser.add_argument("--read-only", action="store_true", help="refuse every change to files")
    parser.add_argument(
        "--deny",
        action="append",
        default=[],
        metavar="PATTERN",
        help=f"hide paths with a component matching PATTERN (repeatable; always denied: {' '.join(DEFAULT_DENY)})",
    )
    parser.add_argument("--max-output", type=int, default=None, help="truncate tool output after N characters")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    args = parser.parse_args(argv)

    # Imported here so `mcpath --help` stays fast.
    from .server import DEFAULT_MAX_OUTPUT, create_server

    vfs = VFS.from_url(args.root, read_only=args.read_only, deny=[*DEFAULT_DENY, *args.deny])
    server = create_server(vfs, max_output=args.max_output or DEFAULT_MAX_OUTPUT)
    if args.transport == "stdio":
        # stdout carries the MCP protocol itself: nothing else may print there.
        server.run(show_banner=False)
    else:
        server.run(transport="http", host=args.host, port=args.port)
