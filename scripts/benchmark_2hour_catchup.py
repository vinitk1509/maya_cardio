"""Full-scale 2-hour backlog catch-up benchmark script.
Generates 5,760,000 samples (2-hour outage equivalent), verifies catch-up completes
well within the 15-minute deadline, and performs 100% sample-level ground-truth reconciliation.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.cloud.app import CloudServer
from src.device.storage import DeviceStorage
from src.device.sync import CloudSyncWorker
from src.mock.biosignal_mock import SignalGenerator
from src.mock.check_ground_truth import GroundTruthChecker


def run_2hour_benchmark(total_samples: int = 5_760_000, seed: int = 42):
    print("=" * 75)
    print("MAYA CARDIO 2-HOUR BACKLOG CATCH-UP RECOVERY BENCHMARK")
    print(f"Target Samples: {total_samples:,} (Two-Hour Outage @ 800 Hz)")
    print(f"Deadline: Must complete within 15 minutes (900 seconds)")
    print(f"Minimum Required Catch-Up Rate: 6,400 samples/second")
    print("=" * 75)

    temp_dir = tempfile.mkdtemp(prefix="mayacardio_2hr_")
    dev_db = os.path.join(temp_dir, "bench_2hr_dev.db")
    cloud_db = os.path.join(temp_dir, "bench_2hr_cloud.db")
    cloud_port = 8098

    session_id = "sess_2hour_bench"
    storage = DeviceStorage(dev_db)
    generator = SignalGenerator(mode="ramp", seed=seed)

    print(f"\n[1/4] Pre-populating {total_samples:,} samples into local device storage...")
    t_gen_start = time.monotonic()
    
    # Insert in 50,000-sample chunks
    chunk_size = 50000
    half_samples = total_samples // 2

    for start_i in range(0, half_samples, chunk_size):
        cnt = min(chunk_size, half_samples - start_i)
        s1_batch = [generator.get_sample(1, start_i + j) for j in range(cnt)]
        s2_batch = [generator.get_sample(2, start_i + j) for j in range(cnt)]
        storage.save_samples_and_losses(session_id, 1, start_i, s1_batch, [])
        storage.save_samples_and_losses(session_id, 2, start_i, s2_batch, [])
        sys.stdout.write(f"\r  Generated: {((start_i + cnt) * 2):,} / {total_samples:,} samples...")
        sys.stdout.flush()

    t_gen_end = time.monotonic()
    print(f"\n  Pre-population complete in {t_gen_end - t_gen_start:.2f}s.")

    # Start Cloud Server
    print(f"\n[2/4] Starting Cloud Server on port {cloud_port}...")
    cloud = CloudServer(port=cloud_port, db_path=cloud_db)
    cloud.start()
    time.sleep(0.5)

    # Initialize sync worker
    sync_worker = CloudSyncWorker(
        storage,
        cloud_url=f"http://127.0.0.1:{cloud_port}",
        poll_interval=0.005
    )
    sync_worker.is_online = True

    print("\n[3/4] Reconnecting network: Starting catch-up upload...")
    t_sync_start = time.monotonic()
    last_log_time = t_sync_start

    while True:
        n = sync_worker.sync_cycle()
        if n == 0:
            break
        now = time.monotonic()
        if now - last_log_time >= 5.0:
            last_log_time = now
            elapsed = now - t_sync_start
            synced_so_far = sync_worker.total_samples_synced
            rate = synced_so_far / max(elapsed, 0.001)
            pct = (synced_so_far / total_samples) * 100.0
            print(f"  [{elapsed:5.1f}s] Synced: {synced_so_far:,} / {total_samples:,} ({pct:5.1f}%) | "
                  f"Throughput: {rate:7.0f} samples/s")

    t_sync_end = time.monotonic()
    total_elapsed = max(t_sync_end - t_sync_start, 0.001)
    avg_rate = total_samples / total_elapsed

    print(f"\n  Catch-up completed in {total_elapsed:.2f}s ({total_elapsed / 60.0:.2f} minutes).")
    print(f"  Average Catch-Up Rate: {avg_rate:,.0f} samples/second.")

    # Step 4: Verify Ground Truth
    print("\n[4/4] Verifying 100% sample-level ground truth and loss accounting...")
    cloud_stats = cloud.storage.get_session_stats(session_id)
    print(f"  Cloud Total Stored: {cloud_stats['samples_in_cloud']:,} samples.")

    export_path = os.path.join(temp_dir, "cloud_2hr_export.jsonl")
    with open(export_path, "w", encoding="utf-8") as f:
        for line in cloud.storage.stream_export(session_id):
            f.write(line)

    checker = GroundTruthChecker(mode="ramp", seed=seed)
    res = checker.verify_export_file(export_path, expected_max_samples=half_samples)

    print("\n" + "=" * 75)
    print("BENCHMARK VERIFICATION RESULTS")
    print("=" * 75)
    print(f"1. Target Backlog Size    : {total_samples:,} samples")
    print(f"2. Time to Reconcile      : {total_elapsed:.2f}s (Official limit: 900.0s)")
    print(f"3. Measured Throughput    : {avg_rate:,.0f} samples/s (Requirement: > 6,400 samples/s)")
    print(f"4. Duplicates Detected    : {res['duplicates']}")
    print(f"5. Silent Losses Detected : {res['silent_losses']}")
    print(f"6. Value Mismatches       : {res['value_mismatches']}")
    print(f"7. Ground Truth Pass      : {res['passed']}")
    print("=" * 75)

    cloud.stop()
    shutil.rmtree(temp_dir, ignore_errors=True)

    if not res["passed"] or total_elapsed > 900.0 or avg_rate < 6400:
        print("FAIL: Catch-up benchmark did not satisfy all official criteria.")
        sys.exit(1)
    else:
        print("SUCCESS: Criterion 6 verified under full-scale parameters.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="2-Hour Backlog Catch-Up Benchmark")
    parser.add_argument("--samples", type=int, default=5_760_000, help="Total samples (default 5,760,000 for 2 hours)")
    parser.add_argument("--seed", type=int, default=42, help="Seed")
    args = parser.parse_args()
    run_2hour_benchmark(total_samples=args.samples, seed=args.seed)
