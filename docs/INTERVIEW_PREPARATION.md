# MayaCardio Software Systems Engineer Assessment: Interview Preparation Guide

**Candidate Assessment Defense:** *Never Lose a Heartbeat*  
**Audience:** Candidate preparing for the 60-minute technical interview with MayaCardio engineering evaluators.

---

## 1. Core Architectural Questions & Defenses

### Q1: Why did you choose SQLite in WAL mode instead of a simple append-only flat file or Redis?
**Answer:**
A bedside medical device requires three non-negotiable guarantees: **atomicity, crash recovery without manual repair, and multi-dimensional queryability** (e.g., retrieving unsynced batches, range queries for 5-second heart rate windows, and streaming exports).
- A flat file append-only log provides sequential writes, but recovering from partial writes during a sudden power loss requires building custom write-ahead headers, crash-checkpoints, and truncation logic.
- Redis is an in-memory datastore that risks data loss on sudden power cuts unless configured with `appendfsync always`, which has severe write amplification and high RAM usage (>100 MB).
- SQLite in **WAL (Write-Ahead Logging)** mode provides ACID guarantees natively. WAL writes are sequential (fast on SD cards), readers do not block writers (collector and sync worker operate concurrently without contention), and the WAL recovery mechanism automatically rolls back uncommitted partial transactions upon restart without manual repair.

---

### Q2: Why is data saved durably on the device disk before streaming to the cloud?
**Answer:**
In a hospital setting, Wi-Fi can drop without warning at any millisecond. If data is streamed over TCP/HTTP before being persisted locally, an in-flight network timeout or TCP reset puts the sample in an ambiguous state: Did the cloud receive it? If not, can we re-read it from the sensor?
By persisting the sample to local disk first inside an atomic transaction:
1. The device becomes the single **durable source of truth**.
2. Network transmission is treated strictly as an **asynchronous projection** of local durable state.
3. If the network drops or the cloud service restarts, the device pipeline never stalls; it seamlessly transitions to offline mode, continues capturing, and accumulates a durable local backlog that is guaranteed to survive reboots.

---

### Q3: What happens during a sudden hardware power cut? What is your maximum loss bound?
**Answer:**
Our system guarantees a maximum potential data loss of **at most 1.0 second of data** (400 samples per sensor).
- **Why at most 1.0 second?** The collector reads data from the mock in batches of at most 1 second. Each batch is inserted inside an atomic SQLite transaction. SQLite transactions are all-or-nothing: if power is cut mid-transaction, SQLite's WAL recovery automatically rolls back the partial transaction upon reboot.
- **Replay Buffer Recovery:** When the device boots, the collector inspects the highest persisted sample index `K` and sends a replay request: `{"cmd": "read", "sensor": S, "from_index": K + 1}`. Because the mock retains a 10-second circular buffer, if the power cut and reboot takes less than 10 seconds, the uncommitted samples are re-read from the mock with **zero loss**.
- If the reboot takes longer than 10 seconds, the mock buffer overflows and explicitly reports the unread range in `lost`. The collector immediately logs this loss in the `losses` table. Under no circumstances is any data silently lost.

---

### Q4: How does CRC-32 verification work, and why couldn't you use `json.loads()` and re-serialize?
**Answer:**
Section 6.2 states that the CRC-32 is computed over the **exact data-array text transmitted**, from the opening `[` to the matching `]`.
If you parse the JSON into Python objects with `json.loads()` and then re-serialize with `json.dumps(data)`, Python's JSON serializer may alter whitespace, comma spacing, or integer formatting (e.g. `[1, 2]` vs `[1,2]`). Any difference in character representation changes the zlib CRC-32 checksum, causing false positive CRC errors.
To prevent this, `src/common/protocol.py` implements `extract_data_array_slice`: it scans the raw byte buffer received over TCP, locates `"data"`, finds the first `[` after the colon, walks bracket depth to the matching `]`, and extracts the exact raw transmitted byte slice. It then computes `zlib.crc32()` directly over those raw bytes.

---

### Q5: How do you achieve exactly-once cloud storage with at-least-once network delivery?
**Answer:**
In distributed systems, networks only provide **at-least-once** delivery: if a client POSTs a batch of samples to the cloud, the cloud commits them, but an ACK packet drops, the client will retry the same batch.
We ensure **exactly-once storage** through idempotent persistence:
1. Every sample has a deterministic natural primary key: `(session_id, sensor, sample_index)`.
2. Both device and cloud use `INSERT OR IGNORE INTO samples ...`.
3. If an upload is retried, duplicate rows are discarded by the database engine with zero errors, zero duplicates, and zero row inflation.

---

### Q6: How does the Section 3 Heart-Rate algorithm work, and why does it require an 800-sample history?
**Answer:**
The ECG peak rule requires 4 conditions for a sample `i` to qualify as a peak:
1. **History & Lookahead:** The 800 samples before it (`i-800` to `i-1`) and 1 sample after it (`i+1`) must be available.
2. **Local Maximum:** `val[i] > val[i-1]` and `val[i] > val[i+1]` (strictly greater than both neighbours).
3. **Dynamic Threshold:** `val[i] > 0.5 * max(val[i-800 ... i-1])`. The 800-sample history (~2.0 seconds at 400 Hz) provides a dynamic rolling baseline so the peak threshold adapts to physiological amplitude changes and avoids mistaking baseline wander or P/T-waves for QRS complexes.
4. **Refractory Period:** `i - prev_peak >= 100` samples (250 ms), reflecting physiological cardiac refractory periods (preventing double-counting a wide R-wave).
- For window `w`, we inspect peaks in the 5-second interval `400*(w-4)` to `400*(w+1)-1`. If any sample in that 2,000-sample window is missing, or `w < 4`, or fewer than 2 peaks exist, `heart_rate` is strictly `null`.
- Peak state is preserved across window boundaries in memory and cache so peaks near 1-second borders are never reset or miscalculated.

---

### Q7: What performance bottlenecks did you discover during benchmarking, and how did you resolve them?
**Answer:**
During initial catch-up throughput benchmarking with a 50,000-sample backlog:
1. **Bottleneck 1:** `CloudSyncWorker` was calling `storage.get_counts()` on every sync iteration. `get_counts()` ran a `SELECT COUNT(*), SUM(CASE...) FROM samples` full-table scan on the 50,000-row table, taking ~15 ms per loop.
   * *Fix:* Replaced full-table scan with an O(1) index query (`SELECT sample_index ... WHERE synced = 0 LIMIT CATCHUP_BATCH_SIZE`).
2. **Bottleneck 2:** `mark_samples_synced` used `cursor.executemany("UPDATE samples SET synced = 1 WHERE session_id = ? AND sensor = ? AND sample_index = ?", 4000_keys)`. Executing 4,000 individual SQL update statements took ~400 ms per batch, capping catch-up throughput at ~4,900 samples/sec.
   * *Fix:* Re-engineered to range-based updates. Because samples are sequential, we group keys by `(session_id, sensor)` and execute a single range query: `UPDATE samples SET synced = 1 WHERE session_id = ? AND sensor = ? AND sample_index >= ? AND sample_index <= ? AND synced = 0`.
   * *Result:* Update time dropped from 400 ms to 2 ms. Catch-up throughput skyrocketed from 4,900 samples/sec to **19,873 samples/second**, more than triple the 6,400 samples/sec requirement.

---

### Q8: What remains imperfect, and what would you improve in a commercial production medical device?
**Answer:**
1. **Hardware-level Flash Protection:** On a real bedside monitor, standard Consumer MicroSD cards can suffer filesystem corruption on dirty power cuts. In production, we would use industrial eMMC with pSLC mode, hardware power-loss protection (supercapacitors giving 100ms of brownout power to flush dirty caches), and a read-only rootfs with an overlayfs journal.
2. **End-to-End Encryption & Device Authentication:** Currently, HTTP REST and TCP mock sockets use plaintext local communication. A clinical production pipeline would enforce mutual TLS (mTLS) with per-device hardware secure element (e.g. ATECC608A) certificates, encrypting data at rest with AES-GCM-256.
3. **Streaming Wire Format:** While JSON Lines is human-readable and required by the assessment specification, a commercial high-frequency telemetry system (e.g., 8-channel 1000 Hz ECG) would use Protocol Buffers or FlatBuffers, reducing serialization CPU overhead and bandwidth by another 60%.
