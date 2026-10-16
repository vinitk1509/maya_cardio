"""Resource utilization benchmark script measuring the four official metrics:
  1. Average CPU utilization (%)
  2. Peak memory consumption (MB)
  3. Megabytes written to disk (MB)
  4. Megabytes sent over the network (MB)

Can run 1-hour test (--duration 3600) or short validation run (--duration 60).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict

import psutil

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.mock.biosignal_mock import BiosignalMockServer
from src.cloud.app import CloudServer
from src.device.pipeline import PipelineService


def run_benchmark(duration_seconds: int = 3600, mock_mode: str = "signal", seed: int = 7) -> Dict[str, Any]:
    temp_dir = tempfile.mkdtemp(prefix="mayacardio_bench_")
    device_db = os.path.join(temp_dir, "device_bench.db")
    cloud_db = os.path.join(temp_dir, "cloud_bench.db")

    mock_port = 7199
    cloud_port = 8099

    print("=" * 70)
    print(f"MAYA CARDIO RESOURCE BENCHMARK ({duration_seconds}s)")
    print(f"Environment: {sys.platform} ({os.name}), Python {sys.version.split()[0]}")
    print(f"Target Mode: {mock_mode}, Seed: {seed}")
    print("=" * 70)

    # 1. Start mock server
    print(f"[1/3] Starting biosignal-mock on port {mock_port}...")
    mock = BiosignalMockServer(
        mode=mock_mode,
        seed=seed,
        duration=duration_seconds + 60,
        port=mock_port,
        realtime=True
    )
    import threading
    mock_thread = threading.Thread(target=mock.run_server, daemon=True)
    mock_thread.start()
    time.sleep(0.5)

    # 2. Start cloud service
    print(f"[2/3] Starting cloud service on port {cloud_port}...")
    cloud = CloudServer(port=cloud_port, db_path=cloud_db)
    cloud.start()
    time.sleep(0.5)

    # 3. Start device pipeline
    print(f"[3/3] Starting device pipeline service...")
    pipeline = PipelineService(
        db_path=device_db,
        mock_port=mock_port,
        cloud_url=f"http://127.0.0.1:{cloud_port}"
    )
    pipeline.start()
    time.sleep(1.0)

    # Monitoring loop
    process = psutil.Process(os.getpid())
    cpu_samples = []
    peak_rss_bytes = 0
    start_time = time.monotonic()
    last_report_time = start_time

    # Record initial disk / db size
    initial_db_size = os.path.getsize(device_db) if os.path.exists(device_db) else 0

    print(f"Telemetry logging active. Running for {duration_seconds} seconds...")

    try:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= duration_seconds:
                break

            # CPU percentage (per-process)
            cpu_pct = process.cpu_percent(interval=1.0)
            cpu_samples.append(cpu_pct)

            # Memory consumption (RSS)
            mem_info = process.memory_info()
            if mem_info.rss > peak_rss_bytes:
                peak_rss_bytes = mem_info.rss

            # Periodic log update every 30 seconds
            if time.monotonic() - last_report_time >= 30.0:
                last_report_time = time.monotonic()
                st = pipeline.get_status()
                curr_cpu = sum(cpu_samples[-30:]) / max(len(cpu_samples[-30:]), 1)
                curr_ram_mb = mem_info.rss / (1024 * 1024)
                print(f"[{elapsed:6.1f}s / {duration_seconds}s] Mode: {st.mode:7s} | "
                      f"Saved: {st.samples_saved:7d} | Backlog: {st.backlog:4d} | "
                      f"CPU: {curr_cpu:4.1f}% | RAM: {curr_ram_mb:5.1f}MB")

    finally:
        pipeline.stop()
        cloud.stop()
        mock.stop()
        time.sleep(0.5)

    # Calculate final resource measurements
    avg_cpu = sum(cpu_samples) / max(len(cpu_samples), 1)
    peak_ram_mb = peak_rss_bytes / (1024 * 1024)

    # Measure total disk written: database file + WAL size
    final_db_size = 0
    for fname in os.listdir(temp_dir):
        if fname.startswith("device_bench.db"):
            fpath = os.path.join(temp_dir, fname)
            final_db_size += os.path.getsize(fpath)
    disk_written_mb = final_db_size / (1024 * 1024)

    # Measure total network bytes sent by device sync worker
    net_sent_mb = pipeline.sync_worker.total_bytes_sent / (1024 * 1024)

    shutil.rmtree(temp_dir, ignore_errors=True)

    report = {
        "duration_seconds": duration_seconds,
        "environment": {
            "platform": sys.platform,
            "architecture": sys.platform,
            "python_version": sys.version.split()[0],
            "cpu_count": psutil.cpu_count(logical=True),
            "total_ram_gb": round(psutil.virtual_memory().total / (1024 ** 3), 2)
        },
        "metrics": {
            "average_cpu_percent": round(avg_cpu, 2),
            "peak_memory_mb": round(peak_ram_mb, 2),
            "disk_megabytes_written": round(disk_written_mb, 2),
            "network_megabytes_sent": round(net_sent_mb, 2)
        },
        "pipeline": {
            "total_samples_saved": pipeline.get_status().samples_saved,
            "total_samples_synced": pipeline.sync_worker.total_samples_synced
        }
    }

    print("\n" + "=" * 70)
    print("FINAL BENCHMARK RESULTS (MANDATORY SUBMISSION FIGURES)")
    print("=" * 70)
    print(f"1. Average CPU Utilization : {report['metrics']['average_cpu_percent']}%")
    print(f"2. Peak Memory Consumption : {report['metrics']['peak_memory_mb']} MB")
    print(f"3. Disk Megabytes Written  : {report['metrics']['disk_megabytes_written']} MB")
    print(f"4. Network Megabytes Sent  : {report['metrics']['network_megabytes_sent']} MB")
    print("=" * 70)

    return report


def main():
    parser = argparse.ArgumentParser(description="Run MayaCardio 1-Hour Resource Benchmark")
    parser.add_argument("--duration", type=int, default=3600, help="Test duration in seconds (default 3600 for 1 hour)")
    parser.add_argument("--mode", choices=["signal", "ramp"], default="signal", help="Mock signal mode")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed")
    parser.add_argument("--output", default="docs/RESOURCE_REPORT.json", help="Path to save JSON report")
    args = parser.parse_args()

    report = run_benchmark(duration_seconds=args.duration, mock_mode=args.mode, seed=args.seed)

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved to {args.output}")


if __name__ == "__main__":
    main()
