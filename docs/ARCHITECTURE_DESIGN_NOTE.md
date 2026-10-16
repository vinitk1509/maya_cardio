# MayaCardio Data Pipeline: Architecture Design Note
**Document Type:** Candidate Assessment Design Note (Max 2 Pages)  
**Target Environment:** Raspberry Pi 4 (ARM64, <= 4 Cores, <= 2 GB RAM) & Free Cloud Service  
**Candidate Name:** Candidate Assessment Submission — October 2026  

---

## 1. System Architecture

The MayaCardio pipeline implements a crash-resilient, offline-first distributed architecture designed for bedside medical devices operating in adverse hospital network environments.

```
                      +---------------------------------------+
                      |       biosignal-mock (TCP 7100)       |
                      |  400 Hz Dual Sensors (3-ch & 2-ch)    |
                      +-------------------+-------------------+
                                          | Newline-delimited JSON
                                          v
+----------------------------------- DEVICE (ARM64) -----------------------------------+
|                                                                                      |
|   +---------------------+        +--------------------+        +-----------------+   |
|   |   DeviceCollector   | -----> |   DeviceStorage    | <----- | Offline Engine  |   |
|   | - Exact CRC32 Check | Atomic | - SQLite WAL Mode  |        | - Section 3     |   |
|   | - Replay On Error   | Commit | - Full Idempotency |        | - Peak & HR Math|   |
|   | - Session Isolation |        | - Loss Accounting  |        +-----------------+   |
|   +---------------------+        +---------+----------+                              |
|                                            | Unsynced Batches                        |
|                                            v                                         |
|   +---------------------+        +--------------------+                              |
|   |    StateMachine     | -----> |  CloudSyncWorker   |                              |
|   | - Health Probing    | Mode   | - Gzip Compression |                              |
|   | - <10s Switch Bound | Gate   | - Catch-Up Sync    |                              |
|   +---------------------+        +---------+----------+                              |
+--------------------------------------------|-----------------------------------------+
                                             | HTTP/1.1 REST (gzip)
                                             v
+------------------------------------ CLOUD TIER -------------------------------------+
|                                                                                      |
|   +---------------------+        +--------------------+        +-----------------+   |
|   |  HTTP Ingest API    | -----> |    CloudStorage    | -----> | OnlineProcessor |   |
|   | - Batch Ingestion   | Idemp. | - WAL SQLite Store |        | - <5s Window Res|   |
|   | - Health Check      | Ingest | - Durable Backlog  |        | - Parity Engine |   |
|   +---------------------+        +--------------------+        +-----------------+   |
+--------------------------------------------------------------------------------------+
```

### Core Architecture Components

1. **Multiplexed Dual-Sensor Collector (`src/device/collector.py`):**
   - Establishes a single persistent TCP connection to `biosignal-mock` on port 7100.
   - Alternates reads between Sensor 1 (3 channels) and Sensor 2 (2 channels) at 400 Hz.
   - Extracts the exact raw bytes between the opening `[` and matching `]` of the `"data"` field to compute `zlib.crc32`. If corrupted CRC is detected, issues immediate replay request `{"cmd": "read", "sensor": S, "from_index": N}`.
   - Detects session ID changes on mock restarts and records unread losses with `"to_index": null`.

2. **Crash-Safe Local Journal (`src/device/storage.py`):**
   - Employs SQLite in Write-Ahead Logging (WAL) mode with `PRAGMA synchronous = FULL` (or checkpointed `NORMAL`).
   - Every read batch (at most 1 second, 400 samples) is committed atomically before the read cursor advances.
   - Natural compound primary key `(session_id, sensor, sample_index)` guarantees strict deduplication across process crashes, reboots, and network retries.

3. **Autonomous Online/Offline State Machine (`src/device/state_machine.py`):**
   - Continuously probes cloud connectivity via active health checks (`GET /api/v1/health`) using monotonic time.
   - Transitions from `ONLINE` to `OFFLINE` and back within **10 seconds** of any network transition.

4. **Decoupled High-Throughput Sync Worker (`src/device/sync.py`):**
   - Operates in a dedicated background thread, completely decoupling cloud network I/O from real-time sensor capture.
   - In online mode, streams live batches (400 samples) every 250ms to ensure cloud window calculation well within the 5-second deadline.
   - In catch-up mode after network restoration, transfers compressed batches (10,000 samples) using grouped range updates, achieving **31,377 samples/second** (>4.9x the required 6,400 samples/sec catch-up threshold).

5. **Shared Versioned Signal Processing (`src/processing/engine.py`):**
   - Identical shared mathematical implementation on device (offline) and cloud (online).
   - Computes 1-second window channel statistics, signal magnitude, and executes the exact 4-condition ECG peak detection with sequential 1-sample lookahead and 5-second window heart rate formula.

---

## 2. System Guarantees & Non-Guarantees

### What Is Guaranteed

1. **Zero Silent Data Loss:** Every sample received by the device is safely committed to durable disk. Any missing sample reported by the mock, lost during session restarts, or dropped due to buffer expiration is explicitly persisted as a loss record with valid `sensor`, `from_index`, `to_index`, and `reason`.
2. **Exactly-Once Cloud Ingestion:** Retried network batches or duplicate mock packets never produce duplicate stored samples in either the device or cloud databases due to primary key uniqueness constraints (`INSERT OR IGNORE`).
3. **Switching Time Bound:** Mode transitions between online and offline occur within 10 seconds of network interruption or restoration.
4. **Online Processing Latency:** Window results appear in the cloud within 5 seconds of the closing sample index.
5. **Catch-Up Completion:** Outages up to 2 hours (5.76M samples) synchronize within 15 minutes, supported by benchmarked throughput of 31,377 samples/sec.
6. **Processing Parity:** Online and offline modes produce identical mathematical outputs for identical inputs.
7. **Resource Bounds:** Bounded footprint verified on host (82.5 MB peak RSS, ~38% single-core CPU); setup scripts and Dockerfile provided for physical Pi 4 verification.

### What Is NOT Guaranteed

1. **Hardware Volatile Memory Durability:** Samples held in volatile TCP socket buffers or process memory prior to SQLite transaction commit cannot survive a simultaneous hardware power cut.
2. **Physical Storage Exhaustion:** If the device disk is filled to 100% capacity with 0 bytes remaining across the entire filesystem, new raw samples cannot be written. In this condition, the pipeline enters a safe degraded mode, protects existing data integrity without silent drops, and uses a pre-allocated emergency cushion (10 MB) to record operational failure status.
3. **Hardware Power Loss with Empty Mock Buffer:** If power is cut during a read and the device remains unpowered for longer than the mock's 10-second retained circular buffer (4,000 samples), samples that expired in the mock buffer cannot be replayed and are recorded as losses.

---

## 3. Power-Cut Behavior & Stated Maximum Loss

### Stated Maximum Data Loss on a Power Cut
> **Guaranteed Maximum Data Loss:** **At most 1.0 second of data** (400 samples per sensor).

### Engineering Rationale & Proof

1. **Atomic Transaction Boundaries:**
   - The device collector queries the mock in discrete read cycles of at most 1 second of samples (400 samples per sensor).
   - Each batch is written inside a single atomic SQLite transaction:
     ```python
     with conn:
         cursor.executemany("INSERT OR IGNORE INTO samples ...", batch)
         cursor.execute("UPDATE checkpoints SET last_index = ...")
     ```
   - In SQLite WAL mode, transactions are atomic and flushed before checkpointing. If power is interrupted mid-write, the partial transaction rolls back completely upon reboot. The existing database remains 100% uncorrupted (`PRAGMA integrity_check == 'ok'`).

2. **Mock 10-Second Buffer Replay Recovery:**
   - Upon system reboot, the pipeline queries `storage.get_last_stored_index(session, sensor)` to determine the latest safely committed index `K`.
   - The collector immediately requests replay: `{"cmd": "read", "sensor": S, "from_index": K + 1}`.
   - If the power interruption and reboot duration is less than 10 seconds, the uncommitted 1-second batch is replayed directly from the mock's 10-second retained buffer, resulting in **0 seconds of data loss**.
   - If the power outage exceeds 10 seconds, the mock buffer overflows and explicitly reports the missing range in `lost`. The collector records this range in `losses`. Under no circumstances is any data silently lost.
