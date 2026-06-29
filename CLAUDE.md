# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Running the Application
```bash
# With config file
python run.py -c config.yml

# With command line arguments
python run.py -c config.yml -u "https://www.douyin.com/video/xxx" -t 8

# As REST API server (requires fastapi + uvicorn)
python run.py --serve --serve-port 8000
```

### Testing
```bash
# Run all tests
python -m pytest tests/

# Run specific test file
python -m pytest tests/test_api_client.py

# Run with quiet output
python -m pytest tests/ -q

# Run with coverage (optional)
python -m pytest tests/ --cov=core --cov=auth --cov=config
```

### Code Quality
```bash
# Lint with ruff
ruff check .

# Format code
ruff format .

# Lint and fix
ruff check . --fix
```

### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Install for development
pip install -e .[dev]

# Install browser support (for cookie fetching)
pip install -e .[browser]

# Install server mode dependencies
pip install -e .[server]
```

## Architecture Overview

This is an async-first Python application for batch downloading Douyin (TikTok China) content. The architecture follows a modular design with clear separation of concerns.

### Core Architecture

1. **Entry Point**: `run.py` bootstraps the environment and delegates to `cli.main:main()`
2. **CLI Layer**: `cli/main.py` handles command-line parsing and orchestrates the download workflow
3. **Core Business Logic**: `core/` contains all downloaders, API clients, and business rules
4. **Authentication**: `auth/` manages cookies, tokens, and login sessions
5. **Control Layer**: `control/` manages rate limiting, retries, and concurrency
6. **Storage Layer**: `storage/` handles file I/O and SQLite database operations
7. **Configuration**: `config/` loads and validates YAML configuration files

### Key Design Patterns

- **Factory Pattern**: `DownloaderFactory.create()` instantiates the appropriate downloader based on URL type
- **Strategy Pattern**: `core/user_modes/` implements different download strategies (post, like, mix, music)
- **Registry Pattern**: `UserModeRegistry` discovers and manages available download modes
- **Async Architecture**: All I/O operations use asyncio (`aiohttp`, `aiofiles`, `aiosqlite`)

### Important Conventions

- All downloaders inherit from `BaseDownloader` and implement `_download_mode_items()`
- Configuration uses YAML with environment variable overrides (`DOUYIN_*` prefix)
- Database operations are async and use `aiosqlite`
- Logging is module-based via `utils.logger.setup_logger(name)`
- Error handling includes automatic relogin on authentication failures

### Shared Logic with Desktop

This project shares Python backend logic with `/Users/crimson/codes/douyin/douyin-downloader-desktop`. When modifying files in `auth/`, `cli/`, `config/`, `control/`, `core/`, `storage/`, `tools/`, or `utils/`:

1. Apply equivalent changes in both projects
2. Use `../douyin-downloader-desktop/scripts/sync-to-cli.sh --check` to detect drift
3. Document intentional divergences (CLI omits desktop-only features like license enforcement)

### Configuration Structure

Key configuration sections:
- `link`: URLs to download
- `mode`: Download modes (post, like, mix, music, collect, collectmix)
- `number`: Per-mode item limits (0 = unlimited)
- `increase`: Enable incremental downloads
- `thread`: Concurrency level
- `database`: Enable SQLite deduplication
- `browser_fallback`: Fallback to browser for pagination
- `transcript`: Optional OpenAI transcription
- `notifications`: Push notifications on completion

### Testing Guidelines

- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- Mock external APIs in tests using `unittest.mock`
- Test both success and failure scenarios
- Include integration tests for the main download flow
- Database tests should use temporary SQLite files