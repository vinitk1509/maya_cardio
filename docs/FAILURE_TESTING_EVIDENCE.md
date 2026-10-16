# MayaCardio Data Pipeline: Failure Testing Evidence & Verification Log

**Deliverable 3:** Evidence of failure tests run, environment details, and pass/fail verification results against the official MayaCardio assessment rubric.

---

## 1. Test Suite Summary Matrix

All tests are automated, reproducible, and verifiable via `python -m pytest tests/`.

| Test ID | Category | Fault Injected / Scenario | Expected Behavior | Actual Measured Outcome | Ground Truth Verification | Status |
|---|---|---|---|---|---|---|
| **FLT-01** | Sensor | Corrupted CRC-32 checksum in mock reply | Reject corrupt reply, issue replay `{"cmd":"read", "from_index": N}`, recover samples | CRC error caught, replay issued, 100% samples recovered | Zero lost samples | **PASS** |
| **FLT-02** | Sensor | Same reply transmitted twice by mock | Ingest first reply, safely ignore second without duplicating primary keys | `INSERT OR IGNORE` safely ingested data with 0 duplicate keys | Zero duplicates | **PASS** |
| **FLT-03** | Sensor | Mock stops sending Sensor 2 (reports in `lost`) | Record loss range with reason `"sensor_offline"` in device storage | Loss record persisted in `losses` table with exact indices | Loss accounted | **PASS** |
| **FLT-04** | Sensor | Mock restarts, starting new session | Isolate session, record unread loss for old session with `"to_index": null` | Old session marked with `to_index: null`, new session begins at index 0 | Zero session contamination | **PASS** |
| **FLT-05** | Network | Cloud service terminated (network severed) | Switch from `ONLINE` to `OFFLINE` within 10 seconds | Mode switched in **3.00 seconds** (well under 10s bound) | Device export clean | **PASS** |
| **FLT-06** | Offline | Pipeline operating with network severed | Continue collecting from mock, persist locally, compute offline window results | Local samples saved, backlog accumulated, offline results computed | 100% stored | **PASS** |
| **FLT-07** | Network | Cloud service restored | Switch from `OFFLINE` to `ONLINE` within 10 seconds | Mode switched in **< 1.0 second** (well under 10s bound) | Seamless failback | **PASS** |
| **FLT-08** | Catch-Up | 50,000 backlog samples queued for sync | Catch-up throughput exceeds official requirement of 6,400 samples/sec | Achieved **19,873 samples/sec** (50,000 samples in 2.516s) | Backlog drained to 0 | **PASS** |
| **FLT-09** | Cloud Export | Cloud export compared with ground truth | Cloud copy contains every sample exactly once with no duplicates or silent losses | Verified by `check_ground_truth.py`: 0 duplicates, 0 silent losses | Exact ground truth match | **PASS** |
| **FLT-10** | Device Crash | Pipeline process killed with `SIGKILL` (no clean shutdown) | Database remains intact, WAL recovers cleanly on restart without corruption | SQLite `PRAGMA integrity_check` returned `ok`; zero corrupted records | Database valid | **PASS** |
| **FLT-11** | Deduplication | Repeated upload retries during network blips | Cloud database does not duplicate samples on retries | Cloud sample count matches device count exactly | Exactly-once storage | **PASS** |
| **FLT-12** | Storage | Disk space exhaustion simulation | Reject writes below safety threshold (10 MB), enter degraded mode | `DiskFullError` safely raised; existing database uncorrupted | No database corruption | **PASS** |
| **FLT-13** | Math | Window w < 4, missing samples, or < 2 peaks | Return `null` for `heart_rate` per Section 3 rules | Strictly returned `null` for all edge cases | Reference match | **PASS** |
| **FLT-14** | Math | ECG 60 bpm signal in minutes 0–2 | Peak detection detects 1 peak every 400 samples; HR = 60.0 bpm | Detected peaks and computed `heart_rate = 60.0 bpm` | Section 3 rule match | **PASS** |

---

## 2. Detailed Failure Injection Scenarios & Evidence Logs

### Test Scenario A: Corrupt CRC32 Reply & Autonomous Replay Recovery
- **Command:** `python -m pytest tests/faults/test_sensor_faults.py::TestSensorFaults::test_corrupt_crc32_recovery`
- **Mechanism:** Mock was instructed to transmit `crc32: "00000000"` with valid data.
- **Log Output:**
  ```text
  [ERROR] device-collector: CRC32 mismatch on sensor 1: CRC32 mismatch: expected 00000000, calculated f9a5bf62. Requesting replay...
  [INFO] biosignal-mock: Accepted replay request for sensor 1 from index 0
  [INFO] device-collector: Replay successful. Saved 400 samples for sensor 1.
  ```
- **Result:** PASS. Replay completed transparently. Zero data lost.

### Test Scenario B: Network Cut, Automatic Failover, and Restoration Catch-Up
- **Command:** `python -m pytest tests/faults/test_network_and_switching.py`
- **Measured Timings:**
  - Online established: `t = 0.0s`
  - Cloud server killed: `t = 2.0s`
  - State machine transition `ONLINE -> OFFLINE`: `t = 5.00s` (**Transition latency: 3.00 seconds**, requirement <= 10.0s)
  - Offline collection active: Backlog grew from 0 to 1,600 samples
  - Cloud server restored: `t = 7.0s`
  - State machine transition `OFFLINE -> ONLINE`: `t = 7.00s` (**Transition latency: < 0.01 seconds**, requirement <= 10.0s)
  - Backlog catch-up completed: Backlog reached 0 in 0.8s
- **Ground Truth Comparison:**
  ```json
  {
    "passed": true,
    "total_lines": 3200,
    "sensor_1_samples": 1600,
    "sensor_2_samples": 1600,
    "duplicates": 0,
    "value_mismatches": 0,
    "silent_losses": 0
  }
  ```
- **Result:** PASS. Complete data preservation with zero duplicates.

### Test Scenario C: 50,000-Sample Catch-Up Throughput Benchmark
- **Command:** `python -m pytest tests/faults/test_catchup_benchmark.py -s`
- **Benchmark Parameters:**
  - Backlog samples: 50,000 samples (25,000 Sensor 1, 25,000 Sensor 2)
  - Compression: Gzip-encoded batch payloads
  - Database updates: Grouped range-based SQL updates on indexed columns
- **Result Output:**
  ```text
  Starting catch-up synchronization...
  Catch-up completed: 50000 samples in 2.516s -> 19873 samples/second
  ```
- **Verdict:** **19,873 samples/second** achieved vs **6,400 samples/second** required (**310% of requirement**). PASS.

### Test Scenario D: Process SIGKILL Recovery
- **Command:** `python -m pytest tests/faults/test_crash_recovery.py`
- **Execution:** Active pipeline process terminated with `os.kill(proc.pid, signal.SIGKILL)`.
- **Integrity Check:**
  - SQLite `PRAGMA integrity_check` -> `ok`
  - Duplicate check -> `0 duplicates`
  - Restart pipeline -> successfully resumed reading next sample index from mock buffer.
- **Verdict:** PASS.
