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
