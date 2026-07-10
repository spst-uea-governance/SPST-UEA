# SPST-UEA Benchmark

Run the deterministic, API-key-free lifecycle benchmark from the repository root:

```powershell
python benchmark/run_benchmark.py
```

The report contains the RFC-0003/RFC-0005 specification identifiers, runtime
version, measured dispatch results, known limitations, and reproducibility
metadata. The benchmark uses isolated local SQLite/WAL storage and does not
call external model providers.
