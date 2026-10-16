"""CLI tool 'pipeline' implementing the mandatory commands required by MayaCardio assessment:
  - pipeline status
  - pipeline export --session ID --from device|cloud
  - pipeline results --session ID
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from typing import Optional

from src.device.storage import DeviceStorage
from src.device.state_machine import StateMachine
from src.common.logging import setup_logging

logger = setup_logging("pipeline-cli")


def get_default_paths():
    db_path = os.environ.get("MAYACARDIO_DB", "device_data.db")
    cloud_url = os.environ.get("MAYACARDIO_CLOUD_URL", "http://127.0.0.1:8080").rstrip("/")
    return db_path, cloud_url


def cmd_status(args):
    """pipeline status: print current mode, samples saved, samples in the cloud, and backlog as one JSON line."""
    db_path, cloud_url = get_default_paths()
    storage = DeviceStorage(db_path)
    state_machine = StateMachine(cloud_url=cloud_url)
    current_mode = state_machine.evaluate_transition().value

    total_saved, in_cloud, backlog = storage.get_counts(args.session if hasattr(args, "session") else None)

    # Output strictly one valid JSON line to stdout
    status_dict = {
        "mode": current_mode,
        "samples_saved": total_saved,
        "samples_in_cloud": in_cloud,
        "backlog": backlog
    }
    sys.stdout.write(json.dumps(status_dict) + "\n")
    sys.stdout.flush()


def cmd_export(args):
    """pipeline export --session ID --from device|cloud: output JSONL samples then losses."""
    session_id = args.session
    source = args.source.lower()
    db_path, cloud_url = get_default_paths()

    if source == "device":
        storage = DeviceStorage(db_path)
        for line in storage.stream_export(session_id):
            sys.stdout.write(line)
        sys.stdout.flush()

    elif source == "cloud":
        export_url = f"{cloud_url}/api/v1/export?session={session_id}"
        try:
            req = urllib.request.Request(export_url, headers={"User-Agent": "MayaCardio-CLI"})
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                for line in resp:
                    sys.stdout.write(line.decode("utf-8"))
            sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"Error exporting from cloud: {e}\n")
            sys.exit(1)
    else:
        sys.stderr.write(f"Invalid --from source '{source}'. Must be 'device' or 'cloud'.\n")
        sys.exit(1)


def cmd_results(args):
    """pipeline results --session ID: output one JSON line per window and mode with Section 3 outputs."""
    session_id = args.session
    db_path, cloud_url = get_default_paths()

    # Query local device storage first
    storage = DeviceStorage(db_path)
    results_found = False
    for line in storage.stream_results(session_id):
        results_found = True
        sys.stdout.write(line)

    # If no local results or if online, also attempt cloud results
    if not results_found:
        results_url = f"{cloud_url}/api/v1/results?session={session_id}"
        try:
            req = urllib.request.Request(results_url, headers={"User-Agent": "MayaCardio-CLI"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                for line in resp:
                    results_found = True
                    sys.stdout.write(line.decode("utf-8"))
        except Exception as e:
            pass

    # If still no results found, but samples exist locally on device, compute from stored samples
    if not results_found:
        from src.processing.engine import SignalProcessingEngine
        engine = SignalProcessingEngine()
        last_s1 = storage.get_last_stored_index(session_id, 1)
        last_s2 = storage.get_last_stored_index(session_id, 2)
        if last_s1 is not None and last_s2 is not None:
            max_idx = min(last_s1, last_s2)
            num_windows = max_idx // 400
            for w in range(num_windows):
                w_start = w * 400
                w_end = w_start + 399
                s1 = storage.get_window_samples(session_id, 1, w_start, w_end)
                s2 = storage.get_window_samples(session_id, 2, w_start, w_end)
                s1_hist = None
                if w >= 4:
                    hist_start = max(0, 400 * (w - 4) - 800)
                    hist_end = 400 * (w + 1)
                    s1_hist = storage.get_window_samples(session_id, 1, hist_start, hist_end)
                res = engine.process_window(
                    session_id=session_id,
                    window=w,
                    mode="offline",
                    sensor1_samples=s1,
                    sensor2_samples=s2,
                    all_sensor1_history=s1_hist
                )
                storage.save_window_result(res)
                sys.stdout.write(json.dumps(res.to_dict()) + "\n")

    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(prog="pipeline", description="MayaCardio pipeline control utility")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. status
    parser_status = subparsers.add_parser("status", help="Print pipeline status as a single JSON line")
    parser_status.add_argument("--session", default=None, help="Optional session ID filter")
    parser_status.set_defaults(func=cmd_status)

    # 2. export
    parser_export = subparsers.add_parser("export", help="Export session data as JSON lines")
    parser_export.add_argument("--session", required=True, help="Session ID to export")
    parser_export.add_argument("--from", dest="source", required=True, choices=["device", "cloud"], help="Data source ('device' or 'cloud')")
    parser_export.set_defaults(func=cmd_export)

    # 3. results
    parser_results = subparsers.add_parser("results", help="Print window processing results as JSON lines")
    parser_results.add_argument("--session", required=True, help="Session ID")
    parser_results.set_defaults(func=cmd_results)

    # 4. run (helper to start pipeline daemon if called via cli)
    parser_run = subparsers.add_parser("run", help="Run device pipeline daemon")
    parser_run.add_argument("--db", default="device_data.db")
    parser_run.add_argument("--mock-host", default="127.0.0.1")
    parser_run.add_argument("--mock-port", type=int, default=7100)
    parser_run.add_argument("--cloud-url", default="http://127.0.0.1:8080")
    
    def cmd_run(args):
        from src.device.pipeline import PipelineService
        srv = PipelineService(db_path=args.db, mock_host=args.mock_host, mock_port=args.mock_port, cloud_url=args.cloud_url)
        srv.start()
        try:
            while True:
                import time
                time.sleep(1.0)
        except KeyboardInterrupt:
            srv.stop()

    parser_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
