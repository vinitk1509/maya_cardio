# MayaCardio 5-Minute Technical Demonstration Script & Video Plan
**Deliverable 5:** Video Demonstration Guide (Max 5 Minutes)  
**Required Scenarios:** Pipeline running, network cut, offline processing, restoration catch-up, process kill (SIGKILL), power cut, and ground-truth sample integrity verification.

---

## 1. Video Sequence & Timeline (4 Minutes 45 Seconds Total)

| Timestamp | Phase / Action | Screen Activity | Narration Focus |
|---|---|---|---|
| **0:00 – 0:45** | System Initialization & Live Online Capture | Terminal 1: Start `biosignal-mock`<br>Terminal 2: Start `pipeline run`<br>Terminal 3: Run `pipeline status` | Explain dual-sensor capture (400 Hz), raw CRC-32 verification, and live online streaming to cloud. |
| **0:45 – 1:30** | Network Cut & Failover (< 10s) | Terminal 2: Kill cloud network (stop cloud service or disable interface).<br>Terminal 3: Watch `pipeline status` switch to `offline`. | Highlight state machine health-probing and prompt switch to offline in < 4 seconds (well within 10s requirement). |
| **1:30 – 2:15** | Offline Continuity & Local Window Results | Terminal 3: Run `pipeline status` (show backlog growing).<br>Run `pipeline results --session <ID>` to show offline heart-rate calculations. | Explain how sensor capture is decoupled from cloud; device computes offline Section 3 window math locally. |
| **2:15 – 3:00** | Network Restoration & High-Throughput Catch-Up | Terminal 2: Restart cloud service.<br>Terminal 3: Watch mode switch back to `online` and backlog rapidly catch up to 0. | Demonstrate catch-up throughput (> 19,000 samples/sec), completing hours of potential backlog within seconds. |
| **3:00 – 3:45** | Process Kill (SIGKILL) & Automatic Resume | Terminal 2: Run `kill -9 <PID>` on active pipeline.<br>Terminal 3: Run `pipeline status` after restarting pipeline. | Demonstrate crash resilience. SQLite WAL guarantees zero database corruption (`PRAGMA integrity_check`). Pipeline resumes from latest checkpoint. |
| **3:45 – 4:15** | Controlled Hardware Power Cut & Reboot | Camera/Screen: Physically disconnect power from Raspberry Pi 4 (or pull VM power).<br>Re-apply power, system boots. | Explain that uncommitted transactions are strictly bounded to at most 1.0 second. On reboot, collector replays from mock 10-second buffer. |
| **4:15 – 4:45** | Ground-Truth Verification & Conclusion | Terminal 3: Run `pipeline export` from device and cloud, then run `check-ground-truth`. | Point to 100% ground-truth pass: 0 duplicates, 0 value mismatches, 0 silent losses. |

---

## 2. Step-by-Step Operator Commands

### Step 1: Start Components (Online Capture)
```bash
# Terminal 1: Start Mock Sensor Server
python3 -m src.mock.biosignal_mock --mode signal --seed 7 --duration 3600 --port 7100

# Terminal 2: Start Pipeline Daemon
pipeline run --db /var/lib/mayacardio/device_data.db --cloud-url http://127.0.0.1:8080

# Terminal 3: Query Status (Online Mode)
pipeline status
# Output: {"mode": "online", "samples_saved": 3200, "samples_in_cloud": 3200, "backlog": 0}
```

### Step 2: Inject Network Cut
```bash
# In cloud terminal or network interface:
# Stop cloud receiver (simulating severed hospital Wi-Fi)
# In Terminal 3, poll status:
watch -n 1 pipeline status
# Observe mode changes to "offline" in ~3 seconds.
# Backlog increments smoothly with zero sample loss.
```

### Step 3: Inspect Offline Results
```bash
# Verify offline window results are being computed and persisted locally:
pipeline results --session <SESSION_ID> | tail -n 5
```

### Step 4: Restore Network & Catch-Up
```bash
# Restart cloud receiver.
# Within 2 seconds, status reports "online" and backlog rapidly drains to 0:
pipeline status
```

### Step 5: Process Kill (`kill -9`)
```bash
# Find pipeline PID and kill abruptly:
pkill -9 -f "pipeline run"

# Verify database integrity:
sqlite3 /var/lib/mayacardio/device_data.db "PRAGMA integrity_check;"
# Output: ok

# Restart pipeline:
pipeline run --db /var/lib/mayacardio/device_data.db &
pipeline status
```

### Step 6: Power Cut Simulation
```bash
# On physical Raspberry Pi 4:
# Pull power plug while capturing. Re-insert power.
# Or in virtualized Linux:
# echo b > /proc/sysrq-trigger (abrupt hard reset)
# System reboots. Service auto-starts via systemd.
pipeline status
```

### Step 7: Verify Ground Truth
```bash
# Export from device:
pipeline export --session <SESSION_ID> --from device > device_export.jsonl

# Export from cloud:
pipeline export --session <SESSION_ID> --from cloud > cloud_export.jsonl

# Run Ground Truth validation:
python3 -m src.mock.check_ground_truth device_export.jsonl --mode signal --seed 7
python3 -m src.mock.check_ground_truth cloud_export.jsonl --mode signal --seed 7
```
