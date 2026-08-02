# EH Archive

EH Archive is the replacement for the legacy scripts in `old/`. It separates
collection, download, validation, preparation, upload and cleanup into
recoverable services driven by PostgreSQL state.

## Quick start

1. Create a Python 3.11+ environment (Conda: `conda create -n eharchive python=3.11`) and install `pip install -e ".[dev]"`; qBittorrent support is part of the base installation.
2. Copy the four `config/*.toml.sample` files to the matching `config/*.toml` paths, then fill in the PostgreSQL, service, crawl and storage settings. The `.toml` files are local-only and are ignored by Git.
3. Run `eharchive db upgrade` (or `python -m eh_archive.cli db upgrade`).
4. Start `eharchive-web` and `eharchive-supervisor` as separate processes.

The complete Chinese setup and operations guide is [docs/usage.zh-CN.md](docs/usage.zh-CN.md).

The Supervisor runs bounded collection/download/validation/upload/cleanup
workers and periodically starts the independent thumbnail regeneration batch.
Run `eharchive thumbnails --config-dir config` manually when an immediate
thumbnail pass is needed.

The `scripts/` directory contains one-time MySQL migration and reconciliation
tools. Runtime code never imports those scripts.
