# 🌌 Aether-Vault

**Aether-Vault** is a high-performance, modular, Dockerized, Git-like version control and registry system specifically designed for algorithmic trading and machine learning frameworks.

It solves the major challenge of versioning the **"Holy Trinity"** of Machine Learning in a single atomic commit:
1. **Code**: Python training, validation, and pipeline scripts.
2. **Model Weights**: Deep learning weights (e.g., `.pt`, `.safetensors`).
3. **Datasets**: Large input data files (e.g., `.csv`, `.parquet`, `.h5`).

---

## ⚡ Architecture

Aether-Vault achieves outstanding speed by bridging Python and C++:

- **C++ Performance Core (`aether_core`)**: Reads multi-gigabyte files in chunks and hashes them in parallel using a C++11 ThreadPool. It bypasses expensive re-hashing by instantly verifying metadata (file sizes & modification times) against the index.
- **Python CLI (`av_cli`)**: Provides the familiar Git-like user experience (`av add`, `av commit`, `av status`). Large artifacts (>50MB) are automatically replaced by Git-LFS style pointer files.
- **FastAPI CAS Server (`av_server`)**: A centralized, Docker-backed Content-Addressable Storage (CAS) backend where massive models and datasets are streamed, securely deduplicated, and persistently stored.

---

## 🛠️ Installation

### 1. Start the Remote Registry (Docker)
Ensure Docker and Docker-Compose are installed on your system.
```bash
# Clone the repository and navigate to it
cd aether-vault

# Start the registry backend and persistent storage
docker-compose up --build -d
```
*The server will be available at `http://localhost:8000`. Your data is safely stored in a persistent local Docker volume.*

### 2. Install the CLI Tool (Locally)
To use the `av` command on your machine, you need to compile the C++ performance core.
*(Note for Windows users: You must have Visual Studio C++ Build Tools installed).*
```bash
pip install -e .
```

---

## 📖 CLI Usage Guide

Using Aether-Vault is just like using Git!

### Initialize a Repository
Navigate to your PyTorch/Machine Learning project and initialize Aether-Vault:
```bash
av init
```

### Configure LFS (Large File) Threshold
Set the file size limit (in Megabytes) above which files are automatically streamed to the remote server and replaced by lightweight pointers locally.
```bash
av config 100
```

### Stage Files
Add your python code, datasets, and compiled weights to the staging index:
```bash
av add src/train.py data/features.parquet weights/epoch_50.safetensors
```
*Aether-Vault will instantly hash your files and flag the `.safetensors` and `.parquet` as LFS Artifacts.*

### Check Workspace Status
Check for modified, staged, or untracked files instantly:
```bash
av status
```

### Atomic Commit with Metrics
Commit your "Holy Trinity" securely into the local `.av` Directed Acyclic Graph (DAG) and push it to the remote FastAPI registry. You can attach trading metrics (like Sharpe Ratio or Drawdown) directly to the commit!
```bash
av commit -m "LSTM tuned on Q2 data" --metric-sharpe 2.45 --metric-drawdown 0.12
```

### Branching & Checkout
Easily switch between different experiments and branches. Aether-Vault will automatically download any missing model weights from the remote Docker server!
```bash
av branch feature-transformers
av checkout feature-transformers

# Or checkout a specific model snapshot:
av checkout a1b2c3d4...
```

---

## 🤝 Contributing
Contributions are welcome! Please ensure that you have C++17 compiler tools installed to test changes to the `aether_core` hashing engine.

---

## 🗺️ Build Phases & Development Walkthrough

Aether-Vault was designed and constructed in four distinct phases, bridging high-performance C++ systems programming with a clean Python developer experience:

### Phase 1: High-Performance C++ Hashing Core (`aether_core`)
* **Objective**: Overcome Python's performance limitations (GIL, slow single-threaded I/O) when hashing multi-gigabyte datasets and models.
* **Key Components**:
  - **Custom SHA-256 Engine** ([sha256.h](src/sha256.h), [sha256.cpp](src/sha256.cpp)): A pure C++17, FIPS 180-4 compliant, thread-safe cryptographic hashing engine.
  - **Header-only ThreadPool** ([thread_pool.h](src/thread_pool.h)): A lock-based worker queue managing thread dispatching.
  - **Parallel Tree-Hashing** ([core.cpp](src/core.cpp)): Divides large files into 8MB chunks, hashes them concurrently across all CPU cores, and hashes the concatenated chunk hashes to compute a final tree hash.
  - **Metadata Cache Validation**: Exposes standard file metadata stats to avoid computing hashes unless file modification times or sizes actually change.
  - **Python Bindings**: Integrated via `pybind11` to expose high-speed functions to Python with zero-overhead type conversion.

### Phase 2: Staging, Pointer Files & CLI CLI Framework (`av_cli`)
* **Objective**: Create the local database model and Git-like CLI commands for staging files and committing the "Holy Trinity".
* **Key Components**:
  - **Staging Index Manager** ([index.py](python/av_cli/index.py)): Manages `.av/index` (JSON format) mapping paths to hashes, sizes, types (`code` vs `artifact`), and staged states.
  - **LFS-Style Pointer Manager** ([pointer.py](python/av_cli/pointer.py)): Automatically detects large files exceeding the threshold, duplicates them to local object storage, and replaces them with an `.av-pointer` text file.
  - **FastAPI Client Session Handler** ([client.py](python/av_cli/client.py)): Handles connection-pooled REST requests to push/pull CAS objects and commits.
  - **Click CLI Entry Point** ([main.py](python/av_cli/main.py)): Builds CLI commands (`init`, `config`, `add`, `status`, `commit`, `branch`, `checkout`) with rich, user-friendly, ANSI-colored output.

### Phase 3: Content-Addressable Storage (CAS) FastAPI Registry (`av_server`)
* **Objective**: Implement the remote registry that acts as the single source of truth for ML weights and code tracking.
* **Key Components**:
  - **Robust CAS Manager** ([storage.py](python/av_server/storage.py)): Automatically deduplicates uploaded files by storing them at paths mapped by their SHA-256 hashes (`/data/objects/xx/xxxxxxxx...`). Implements atomic writes via temporary files to prevent data corruption.
  - **FastAPI REST Endpoints** ([server.py](python/av_server/server.py)): Handles high-concurrency streaming uploads, downloads, commit registration, reference updates, and server telemetry.

### Phase 4: Integration, Testing & Concurrency Stabilization
* **Objective**: Ensure absolute robustness against race conditions, memory leaks, and edge-cases.
* **Key Components**:
  - **Thread-Safety & Leak Fixes**: Audited and eliminated static state bottlenecks in C++ hashing, fixed memory leaks in thread-pool callbacks, and added thread locks to server uploads.
  - **Comprehensive Test Suite** ([test_vault.py](tests/test_vault.py)): End-to-end integration tests using `pytest` mock client sessions, verifying indices, pointer creation, DAG integrity, and local/remote staging flows.
  - **Multi-Stage Docker Setup** ([Dockerfile](Dockerfile)): Compiles the C++ wheels in a builder image and keeps the runtime image clean and lightweight.

---

## Development Roadmap

This roadmap outlines planned phases to migrate the platform to a database-backed, multi-user environment suitable for small-to-medium research teams:

### Phase 1: Database and Cache Configuration
- [ ] Add PostgreSQL container to `docker-compose.yml` with persistent volume mount configurations.
- [ ] Add Redis container to `docker-compose.yml` to serve as a fast metadata access layer.
- [ ] Incorporate database drivers (`sqlalchemy`, `asyncpg`, and `redis-py`) into the server package configuration requirements.

### Phase 2: Database Schema and Cache Integration
- [ ] Implement SQL schemas for the commits table, references table, and file catalog index.
- [ ] Refactor the server module to write and read commits directly from PostgreSQL databases instead of local JSON file systems.
- [ ] Integrate a write-through Redis caching pattern to store active object lists, bypassing local disk lookups during file verification checks.

### Phase 3: CLI Exception and Logging Refactoring
- [ ] Replace traceback printing in the Click CLI with structured custom exceptions (`AetherVaultException`) to ensure user-friendly error output.
- [ ] Add configurable logging levels (`--verbose` and `--silent` flags) to the standard command interface.

### Phase 4: Reference Synchronization and Collaboration
- [ ] Build endpoints to push, pull, and synchronize branch references directly between remote team environments.
- [ ] Implement row-level PostgreSQL locks on branch tables to handle concurrent commits from multiple researchers safely.
