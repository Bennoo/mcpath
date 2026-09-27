# Stage 1: install Node.js 22 + OpenAI Codex globally, then bundle into a
# tarball. We use tar because COPY --from dereferences single-file symlinks,
# which breaks npm/npx/codex (they are symlinks into node_modules). tar
# preserves them as symlinks; extraction in stage 2 restores the layout.
FROM node:22-bookworm-slim AS node-installer
RUN npm install -g @openai/codex \
    && tar -czf /tmp/node-runtime.tar.gz \
        -C / \
        usr/local/bin/node \
        usr/local/bin/npm \
        usr/local/bin/npx \
        usr/local/bin/codex \
        usr/local/lib/node_modules

# Stage 2: final dev container image.
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# Install bubblewrap so Codex uses the OS-managed sandbox helper instead of
# its vendored fallback (security updates come via apt this way).
# The base image preconfigures a Yarn apt repo whose signing key is expired
# (NO_PUBKEY 62D54FD4003F6525), which makes apt-get update fail. We don't
# use Yarn here, so just remove the source before updating.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Pull the tarball over and extract; symlinks land intact.
# Both images are Debian Bookworm/arm64, so the node binary is ABI-compatible.
COPY --from=node-installer /tmp/node-runtime.tar.gz /tmp/node-runtime.tar.gz
RUN tar -xzf /tmp/node-runtime.tar.gz -C / \
    && rm /tmp/node-runtime.tar.gz \
    && node --version && npm --version && codex --version

# Install uv system-wide so every user (and image layers) can use it.
# UV_INSTALL_DIR overrides uv's default (~/.local/bin) for a shared location.
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_UNMANAGED_INSTALL=1 sh \
    && uv --version

# Share uv's downloaded Python toolchains across users by putting them in
# a world-readable dir instead of per-user $HOME.
ENV UV_PYTHON_INSTALL_DIR=/usr/local/uv/python
RUN mkdir -p "$UV_PYTHON_INSTALL_DIR" && chmod 0755 /usr/local/uv

# Pre-fetch the project's Python toolchain. Baking it into the image
# avoids a slow first-boot download.
RUN uv python install 3.14 \
    && chmod -R a+rX "$UV_PYTHON_INSTALL_DIR"

# Pre-seed Codex's MCP server config. Codex reads ~/.codex/config.toml on
# startup; baking it into the image means MCPs are wired up out of the box.
RUN mkdir -p /home/vscode/.codex && chown vscode:vscode /home/vscode/.codex
COPY --chown=vscode:vscode codex-config.toml /home/vscode/.codex/config.toml

# Install Claude Code (Anthropic's CLI) via the native installer.
# Standalone binary, no Node.js required. Installer drops it at
# $HOME/.local/bin/claude, which is already on PATH for the vscode user.
USER vscode
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && /home/vscode/.local/bin/claude --version
USER root
