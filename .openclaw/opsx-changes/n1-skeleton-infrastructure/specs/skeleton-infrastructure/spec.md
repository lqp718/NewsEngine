# Spec: skeleton-infrastructure

## ADDED Requirements

### Requirement: Project SHALL have a complete directory tree matching the defined structure

The project root SHALL contain all directories and placeholder files defined in the skeleton layout. Every Python package directory MUST contain an `__init__.py` file. Business module files SHALL be empty (placeholder for future implementation). Test modules MUST follow mirror structure under `tests/`.

#### Scenario: Directory tree is created
- **WHEN** the skeleton setup script is executed
- **THEN** all directories listed in the skeleton layout SHALL exist
- **AND** every Python package directory SHALL contain an `__init__.py`
- **AND** all business module files SHALL exist as empty files

#### Scenario: Verification of existing files is preserved
- **WHEN** setup runs on a project with pre-existing files (`.env`, `docker-compose.yml`, `test_graphiti_episode.py`)
- **THEN** setup SHALL NOT modify or overwrite these files
- **AND** the pre-existing file content SHALL remain byte-identical

---

### Requirement: Neo4j container SHALL be running with correct configuration

The Neo4j 5 Community Edition database SHALL run as a Docker container named `newsengine-neo4j`. The container SHALL expose ports 7474 (HTTP/UI) and 7687 (Bolt protocol). JVM heap SHALL be configured with a maximum of 2GB and an initial size of 512MB. Page cache SHALL be set to 512MB. The container SHALL auto-restart on failure (`unless-stopped`).

#### Scenario: Container starts successfully
- **WHEN** `docker compose up -d` is executed
- **THEN** a container named `newsengine-neo4j` SHALL enter running state within 60 seconds
- **AND** the container SHALL pass its healthcheck

#### Scenario: Bolt connection is available
- **WHEN** Neo4j container is running
- **THEN** a connection to `bolt://localhost:7687` SHALL succeed with credentials from `.env`

#### Scenario: Browser UI is accessible
- **WHEN** Neo4j container is running
- **THEN** HTTP GET to `http://localhost:7474` SHALL return HTTP 200

---

### Requirement: Python dependencies SHALL be installable via requirements.txt

A `requirements.txt` file SHALL exist at the project root containing all required Python packages with pinned version constraints. Running `pip install -r requirements.txt` in the project's virtual environment SHALL complete without errors. The `graphiti_core` package SHALL be importable after installation.

#### Scenario: pip install completes successfully
- **WHEN** `pip install -r requirements.txt` is executed in the virtual environment
- **THEN** all packages SHALL install without error
- **AND** the exit code SHALL be 0

#### Scenario: graphiti_core is importable
- **WHEN** Python interpreter is invoked with `import graphiti_core`
- **THEN** the import SHALL succeed without ModuleNotFoundError

#### Scenario: existing test script still works
- **WHEN** the pre-existing `test_graphiti_episode.py` is executed
- **THEN** it SHALL run without error (skipping any Neo4j-dependent test is acceptable if DB is unavailable)

---

### Requirement: Configuration template SHALL be available via .env.example

A `.env.example` file SHALL exist at the project root, derived from the existing `.env` file. All sensitive values (API keys, passwords) SHALL be replaced with placeholder strings. Non-sensitive configuration keys (URLs, model names, provider identifiers) SHALL remain unchanged. The file SHALL include section comments for clarity.

#### Scenario: .env.example is generated
- **WHEN** setup script generates `.env.example` from `.env`
- **THEN** the file SHALL contain all keys from `.env`
- **AND** the `BAILIAN_API_KEY` value SHALL be `your-api-key-here`
- **AND** the `NEO4J_PASSWORD` value SHALL be `your-neo4j-password-here`
- **AND** the `OPENAI_BASE_URL` value SHALL remain `https://dashscope.aliyuncs.com/compatible-mode/v1`

---

### Requirement: .gitignore SHALL exclude sensitive and generated files

A `.gitignore` file SHALL exist at the project root. It MUST exclude: `.env` (secrets), `data/neo4j/` and `data/logs/` (data volumes), `logs/*.log` and `logs/*.json` (application logs), `.venv/` (virtual environment), `__pycache__/` and `*.pyc` (compiled Python), IDE settings (`.vscode/`, `.idea/`, `.swp`, `.swo`), OS artifacts (`.DS_Store`, `Thumbs.db`), and test caches (`.pytest_cache/`).

#### Scenario: git status ignores sensitive files
- **WHEN** `.gitignore` is in place and `git status` is run
- **THEN** `.env` SHALL NOT appear as an untracked file
- **AND** `data/neo4j/` contents SHALL NOT appear
- **AND** `.venv/` SHALL NOT appear

---

### Requirement: Implementation plan document SHALL be linked from docs/

A `docs/` directory SHALL exist at the project root containing a reference to the project implementation plan. The plan document SHALL be symlinked or copied from `SynapseEngine/docs/NEWSENGINE-IMPLEMENT-PLAN.md`.

#### Scenario: docs directory exists with plan
- **WHEN** setup is complete
- **THEN** `docs/NEWSENGINE-IMPLEMENT-PLAN.md` SHALL exist
- **AND** the file SHALL be non-empty
