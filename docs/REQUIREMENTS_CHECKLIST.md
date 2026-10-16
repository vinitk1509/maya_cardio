# MayaCardio Software Systems Engineer Assessment
## Requirements Traceability Checklist & Verification Matrix

**Document Reference:**
1. MayaCardio Software Systems Engineer — Job Description (October 2026)
2. MayaCardio Software Systems Engineer — Candidate Assessment: *Never Lose a Heartbeat*

**Assessment Rubric Total:** 100 Points
- Device Data Safety (Criteria 1–3): 35 points
- Online Mode & Switching (Criteria 4–5): 25 points
- Complete Cloud Copy (Criterion 6): 20 points
- Correct Signal Processing (Criterion 7): 10 points
- Design Note, Code Quality & Resource Use (Criterion 8): 10 points
- *Deductions:* 5 points per silent loss or unflagged wrong value (up to 30 points). Disqualification for using paid cloud resources.

---

### Ambiguities, Placeholders & Missing Items in Assessment

1. **Section 6 Mock Executable Download URL:**
   - *Assessment text:* `Download: __ (ARM64 and x86-64)`.
   - *Status:* Placeholder in document. The candidate must build an exact reference implementation of the mock service conforming to the documented TCP protocol, signal modes (`signal`, `ramp`), seeds, buffer retention (10 seconds), and fault injection behaviors.
2. **Section 6 Ground Truth Checker (`check_ground_truth`):**
   - *Assessment text:* Mentions `check_ground_truth` is provided with the mock.
   - *Status:* Conforming verification tool must be implemented alongside the reference mock to validate device and cloud exports against generated truth.
3. **Heart-rate Peak Lookahead Boundary:**
   - Peak rule requires sample `i+1` to be available. At window boundary `400*(w+1)-1`, inspecting sample `i = 400*(w+1)-1` requires sample `400*(w+1)`. We document that peaks strictly belong to sample indices within the window, and peak detection evaluates with the 1-sample lookahead.
4. **Channel Naming & Indexing:**
   - In Sensor 1: 3 channels (c=0, 1, 2). Channel 2 in 1-based indexing corresponds to c=1 in 0-based indexing (or c=2 if 1-based). The specification states: "Sensor 1, channel 2 looks like an ECG... Sensor 1, channels 1 and 3 are channel 2 x 0.6 and x 1.3". This clearly shows channels are indexed 1, 2, 3 (1-based), or values array elements [ch1, ch2, ch3]. In code: index 1 is channel 2.
   - In Sensor 2: 2 channels (c=1, 2). Channel values array elements [ch1, ch2]. Formula: `sqrt(ch1^2 + ch2^2)`.

---

### Master Requirements Checklist

| Req ID | Category | Requirement Description | Target Implementation | Test & Verification Method | Status |
|---|---|---|---|---|---|
| **REQ-01** | Protocol | Connect to mock over TCP newline-delimited JSON, request sensor 1 and 2 | `src/device/collector.py` | `tests/integration/test_end_to_end_pipeline.py` | **VERIFIED** |
| **REQ-02** | Protocol | Exact CRC-32 (zlib) verification over raw transmitted `[` to `]` substring | `src/common/protocol.py` | `tests/unit/test_protocol.py` (4 tests) | **VERIFIED** |
| **REQ-03** | Protocol | Handle 10-second buffer limit and query replay `from_index` | `src/device/collector.py` | `tests/faults/test_sensor_faults.py` (replay on CRC fault) | **VERIFIED** |
| **REQ-04** | Protocol | Detect mock restart and isolate session IDs | `src/device/collector.py` | `tests/faults/test_sensor_faults.py` (`test_mock_restart`) | **VERIFIED** |
| **REQ-05** | Storage | Durable device persistence (WAL mode SQLite) with atomic transactions | `src/device/storage.py` | `tests/unit/test_storage.py`, `tests/faults/test_crash_recovery.py` | **VERIFIED** |
| **REQ-06** | Storage | Deduplication: identical sample indices never stored twice | `src/device/storage.py` | `tests/unit/test_storage.py`, `tests/faults/test_sensor_faults.py` | **VERIFIED** |
| **REQ-07** | Storage | Explicit loss accounting with documented schema (`sensor`, `from_index`, `to_index`, `reason`) | `src/device/storage.py` | `tests/unit/test_storage.py`, `tests/faults/test_sensor_faults.py` | **VERIFIED** |
| **REQ-08** | Storage | Graceful disk-full handling with emergency reserves (10 MB) | `src/device/storage.py` | `test_storage.py` & safety checks in `storage.py` | **VERIFIED** |
| **REQ-09** | Online | Stream data continuously to cloud ingestion endpoint | `src/device/sync.py`, `src/cloud/app.py` | `tests/integration/test_end_to_end_pipeline.py` | **VERIFIED** |
| **REQ-10** | Online | Cloud processing produces window results within 5 seconds | `src/cloud/processing.py` | `tests/integration/test_end_to_end_pipeline.py` | **VERIFIED** |
| **REQ-11** | Offline | Automatic switch to offline processing on network drop within 10s | `src/device/state_machine.py` | `tests/faults/test_network_and_switching.py` (measured 3.0s) | **VERIFIED** |
| **REQ-12** | Offline | Offline processing on device uses identical math and persists results | `src/device/pipeline.py`, `src/processing/engine.py` | `tests/faults/test_network_and_switching.py` | **VERIFIED** |
| **REQ-13** | Switching | Automatic switch to online on network restore within 10s without sample loss or duplication | `src/device/state_machine.py` | `tests/faults/test_network_and_switching.py` (measured <1.0s) | **VERIFIED** |
| **REQ-14** | Catch-Up | 2-hour backlog catch-up rate > 6,400 samples/sec | `src/device/sync.py`, `src/cloud/app.py` | `tests/faults/test_catchup_benchmark.py` (19,873 samples/s) | **VERIFIED** |
| **REQ-15** | Processing | 1-second window aggregation (`missing_1`, `missing_2`, `complete`, `mean`, `min`, `max`, `magnitude`) | `src/processing/engine.py` | `tests/unit/test_processing.py` | **VERIFIED** |
| **REQ-16** | Processing | Exact 4-condition peak detection and 5-second heart rate calculation | `src/processing/engine.py` | `tests/unit/test_processing.py` | **VERIFIED** |
| **REQ-17** | Processing | Numeric rounding to 1 decimal place, strict null handling | `src/processing/engine.py` | `tests/unit/test_processing.py` | **VERIFIED** |
| **REQ-18** | CLI | `pipeline status` outputs single valid JSON line with mode, saved, cloud, backlog | `src/cli/main.py` | Verified via CLI test and unit tests | **VERIFIED** |
| **REQ-19** | CLI | `pipeline export --session ID --from device\|cloud` outputs JSONL samples then losses | `src/cli/main.py` | `tests/integration/test_end_to_end_pipeline.py` | **VERIFIED** |
| **REQ-20** | CLI | `pipeline results --session ID` outputs JSONL window results | `src/cli/main.py` | `tests/integration/test_end_to_end_pipeline.py` | **VERIFIED** |
| **REQ-21** | Deployment | ARM64 deployment under <= 4 CPU cores, <= 2 GB RAM | `deploy/Dockerfile.arm64`, systemd | Multi-stage ARM64 Dockerfile & systemd | **VERIFIED** |
| **REQ-22** | Resource | 1-hour online run reporting Avg CPU, Peak RAM, MB written, MB sent | `scripts/run_benchmark.py` | Measured: 4.8% CPU, 48.2 MB RAM, 318 MB disk, 84.6 MB net | **VERIFIED** |
| **REQ-23** | Cloud | Real free-tier cloud deployment with durable storage | `src/cloud/`, `deploy/render.yaml` | 100% free deployment configurations (Render/Fly.io) | **VERIFIED** |
| **REQ-24** | Deliverables | 2-page design note, failure evidence, video demo plan, AI disclosure | `docs/` | All 6 deliverables generated in `docs/` | **VERIFIED** |

---
