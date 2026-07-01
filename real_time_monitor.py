#!/usr/bin/env python3
"""Real-time monitoring of tantan agent with live updates."""

import json
import requests
import time
import sys

API_BASE = "http://127.0.0.1:8000"

def get_status():
    try:
        resp = requests.get(f"{API_BASE}/agent/android/tantan/status", timeout=5)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

def print_compact_status():
    """Print a single line status update."""
    status = get_status()

    if "error" in status:
        return f"[ERROR] {status['error']}"

    running = status.get("running", False)
    stage = status.get("stage", "UNKNOWN")
    sent = status.get("sent_count", 0)
    draft = status.get("draft_count", 0)
    contact = status.get("contact_count", 0)
    last_contact = status.get("last_contact_name", "")

    return f"[{stage}] Running: {running}, Sent: {sent}, Draft: {draft}, Contacts: {contact}, Last: {last_contact}"

def main():
    print("Real-time tantan agent monitor")
    print("=" * 80)

    last_sent = 0
    last_stage = ""
    start_time = time.time()

    while True:
        status = get_status()

        if "error" in status:
            print(f"ERROR: {status['error']}")
            time.sleep(5)
            continue

        current_sent = status.get("sent_count", 0)
        current_stage = status.get("stage", "UNKNOWN")
        running = status.get("running", False)

        # Print header
        elapsed = int(time.time() - start_time)
        print(f"\n[{elapsed}s elapsed] {print_compact_status()}")

        # Show last few logs if stage changed
        if current_stage != last_stage or current_sent > last_sent:
            logs = status.get("logs", [])
            if logs:
                print("  Latest logs:")
                for log in logs[-2:]:
                    time_str = log.get("time", "")[-8:]  # Just show HH:MM:SS
                    msg = log.get("message", "")
                    status_icon = "✓" if log.get("ok") else "✗"
                    print(f"    [{time_str}] {status_icon} {msg}")

        last_sent = current_sent
        last_stage = current_stage

        # Check if we've reached the goal
        if current_sent >= 25:
            print("\n" + "🎉 " * 10)
            print("REACHED 25 SUCCESSFUL REPLIES!")
            print("🎉 " * 10)
            return 0

        # Check if agent has stopped without reaching goal
        if not running and current_sent > 0:
            print(f"\n⚠️  Agent stopped. Progress: {current_sent}/25")
            error = status.get("last_error", "")
            if error:
                print(f"Last error: {error}")
            return 1

        # Wait a bit before next check
        time.sleep(2)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped by user")
        sys.exit(0)
