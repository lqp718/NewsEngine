# Tasks

## 1. Directory Skeleton
- [ ] 1.1 Create `src/` package tree with `__init__.py` and empty module files
  → ref: specs/skeleton-infrastructure/spec.md §Project SHALL have a complete directory tree matching the defined structure
- [ ] 1.2 Create `tests/` package tree with `__init__.py` mirrors
  → ref: specs/skeleton-infrastructure/spec.md §Project SHALL have a complete directory tree matching the defined structure
- [ ] 1.3 Create `logs/` directory with `.gitkeep`
  → ref: specs/skeleton-infrastructure/spec.md §Project SHALL have a complete directory tree matching the defined structure
- [ ] 1.4 Ensure `data/neo4j/` and `data/logs/` directories exist
  → ref: infra

## 2. Configuration Files
- [ ] 2.1 Create `requirements.txt` with all dependency specifications
  → ref: specs/skeleton-infrastructure/spec.md §Python dependencies SHALL be installable via requirements.txt
- [ ] 2.2 Generate `.env.example` from `.env` with sensitive values replaced
  → ref: specs/skeleton-infrastructure/spec.md §Configuration template SHALL be available via .env.example
- [ ] 2.3 Create `.gitignore` with all exclusion rules
  → ref: specs/skeleton-infrastructure/spec.md §.gitignore SHALL exclude sensitive and generated files
- [ ] 2.4 Copy/symlink `docs/NEWSENGINE-IMPLEMENT-PLAN.md` from SynapseEngine
  → ref: specs/skeleton-infrastructure/spec.md §Implementation plan document SHALL be linked from docs/

## 3. Infrastructure Setup
- [ ] 3.1 Start Neo4j container via `docker compose up -d`
  → ref: specs/skeleton-infrastructure/spec.md §Neo4j container SHALL be running with correct configuration
- [ ] 3.2 Verify Neo4j Bolt connectivity (port 7687)
  → ref: specs/skeleton-infrastructure/spec.md §Neo4j container SHALL be running with correct configuration
- [ ] 3.3 Verify Neo4j HTTP/Browser accessibility (port 7474)
  → ref: specs/skeleton-infrastructure/spec.md §Neo4j container SHALL be running with correct configuration
- [ ] 3.4 Install Python dependencies via `pip install -r requirements.txt`
  → ref: specs/skeleton-infrastructure/spec.md §Python dependencies SHALL be installable via requirements.txt

## 4. Verification
- [ ] 4.1 Verify `import graphiti_core` succeeds in the project venv
  → ref: specs/skeleton-infrastructure/spec.md §Python dependencies SHALL be installable via requirements.txt
- [ ] 4.2 Verify `.env` and `test_graphiti_episode.py` remain unmodified
  → ref: specs/skeleton-infrastructure/spec.md §Project SHALL have a complete directory tree matching the defined structure
- [ ] 4.3 Verify `git status` excludes `.env`, `data/`, `.venv/`, `__pycache__`
  → ref: specs/skeleton-infrastructure/spec.md §.gitignore SHALL exclude sensitive and generated files
- [ ] 4.4 Full directory tree audit against specification
  → ref: specs/skeleton-infrastructure/spec.md §Project SHALL have a complete directory tree matching the defined structure
