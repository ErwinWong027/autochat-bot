#!/usr/bin/env python3
"""Monitor tantan agent execution and validate acceptance criteria."""

import json
import requests
import sqlite3
import time
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

API_BASE = "http://127.0.0.1:8000"
DB_PATH = "/Users/erwinwong/Documents/autochat-bot/social_twin.db"

def get_agent_status() -> Dict[str, Any]:
    """Get current tantan agent status."""
    try:
        resp = requests.get(f"{API_BASE}/agent/android/tantan/status", timeout=5)
        return resp.json()
    except Exception as e:
        return {"error": str(e), "running": False}

def query_db(sql: str, params: tuple = ()) -> List[Dict]:
    """Query database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        print(f"DB Error: {e}")
        return []
    finally:
        conn.close()

def get_sent_contacts() -> List[Dict]:
    """Get all contacts that received replies from tantan."""
    sql = """
    SELECT DISTINCT tm.platform_contact_id as contact_id,
           t.display_name,
           COUNT(CASE WHEN tm.role = 'out' THEN 1 END) as reply_count
    FROM thread_messages tm
    JOIN threads t ON t.thread_id = tm.thread_id
    WHERE t.platform = 'tantan' AND tm.role = 'out'
    GROUP BY tm.platform_contact_id
    ORDER BY reply_count DESC
    """
    return query_db(sql)

def check_profile_raw_text(contact_id: str) -> Optional[str]:
    """Check if contact has raw profile text stored."""
    sql = """
    SELECT evidence FROM thread_profile_evidence
    WHERE thread_id IN (
        SELECT thread_id FROM threads
        WHERE platform = 'tantan' AND platform_contact_id = ?
    )
    AND field = 'raw_evidence'
    LIMIT 1
    """
    result = query_db(sql, (contact_id,))
    return result[0]['evidence'] if result else None

def check_llm_prompts(contact_id: str) -> Dict[str, Any]:
    """Check if contact has LLM input records."""
    sql = """
    SELECT last_llm_prompts_json FROM profile_audits
    WHERE contact_id = ?
    """
    result = query_db(sql, (contact_id,))
    if result:
        try:
            prompts = json.loads(result[0]['last_llm_prompts_json'] or '{}')
            return prompts
        except:
            return {}
    return {}

def validate_acceptance() -> Dict[str, Any]:
    """Validate all acceptance criteria."""
    result = {
        "timestamp": datetime.now().isoformat(),
        "criteria": {
            "criterion_1": {"name": "25 successful replies", "status": "PENDING"},
            "criterion_2": {"name": "All profiles have raw text", "status": "PENDING"},
            "criterion_3": {"name": "All replies have LLM records", "status": "PENDING"},
        },
        "details": {
            "sent_contacts": [],
            "total_sent": 0,
            "profiles_missing_raw_text": [],
            "contacts_missing_llm_records": [],
        }
    }

    sent_contacts = get_sent_contacts()
    result["details"]["total_sent"] = len(sent_contacts)

    # Criterion 1: 25 successful replies
    if len(sent_contacts) >= 25:
        result["criteria"]["criterion_1"]["status"] = "PASS"
        result["criteria"]["criterion_1"]["message"] = f"Successfully replied to {len(sent_contacts)} contacts"
    else:
        result["criteria"]["criterion_1"]["status"] = "PENDING"
        result["criteria"]["criterion_1"]["message"] = f"Progress: {len(sent_contacts)}/25 contacts"

    # Check each contact for criteria 2 and 3
    all_profiles_ok = True
    all_llm_records_ok = True

    for contact in sent_contacts:
        contact_id = contact['contact_id']
        result["details"]["sent_contacts"].append({
            "contact_id": contact_id,
            "display_name": contact['display_name'],
            "reply_count": contact['reply_count']
        })

        # Check raw profile text
        raw_text = check_profile_raw_text(contact_id)
        if not raw_text:
            all_profiles_ok = False
            result["details"]["profiles_missing_raw_text"].append(contact_id)

        # Check LLM prompts
        llm_prompts = check_llm_prompts(contact_id)
        required_keys = {'profile_text_analysis', 'decision_making', 'reply_generation'}
        missing_keys = required_keys - set(llm_prompts.keys())

        if missing_keys or not llm_prompts:
            all_llm_records_ok = False
            result["details"]["contacts_missing_llm_records"].append({
                "contact_id": contact_id,
                "missing_keys": list(missing_keys),
                "available_keys": list(llm_prompts.keys())
            })

    # Criterion 2: All profiles have raw text
    if len(sent_contacts) >= 25 and all_profiles_ok:
        result["criteria"]["criterion_2"]["status"] = "PASS"
        result["criteria"]["criterion_2"]["message"] = "All contacts have raw profile text"
    elif len(sent_contacts) >= 25:
        result["criteria"]["criterion_2"]["status"] = "FAIL"
        result["criteria"]["criterion_2"]["message"] = f"Missing raw text: {len(result['details']['profiles_missing_raw_text'])} contacts"
    else:
        result["criteria"]["criterion_2"]["status"] = "PENDING"
        result["criteria"]["criterion_2"]["message"] = f"Checking {len(sent_contacts)} contacts so far"

    # Criterion 3: All replies have LLM records
    if len(sent_contacts) >= 25 and all_llm_records_ok:
        result["criteria"]["criterion_3"]["status"] = "PASS"
        result["criteria"]["criterion_3"]["message"] = "All contacts have complete LLM records"
    elif len(sent_contacts) >= 25:
        result["criteria"]["criterion_3"]["status"] = "FAIL"
        result["criteria"]["criterion_3"]["message"] = f"Missing LLM records: {len(result['details']['contacts_missing_llm_records'])} contacts"
    else:
        result["criteria"]["criterion_3"]["status"] = "PENDING"
        result["criteria"]["criterion_3"]["message"] = f"Checking {len(sent_contacts)} contacts so far"

    # Overall status
    statuses = [c["status"] for c in result["criteria"].values()]
    if all(s == "PASS" for s in statuses):
        result["overall_status"] = "PASS"
    elif any(s == "FAIL" for s in statuses):
        result["overall_status"] = "FAIL"
    else:
        result["overall_status"] = "IN_PROGRESS"

    return result

def print_status(agent_status: Dict, validation: Dict) -> None:
    """Print formatted status."""
    print("\n" + "="*80)
    print(f"[{validation['timestamp']}]")
    print("="*80)

    if agent_status.get("error"):
        print(f"❌ Agent Error: {agent_status['error']}")
    else:
        print(f"Agent Status: {agent_status.get('stage', 'UNKNOWN')} (code: {agent_status.get('status_code')})")
        print(f"  - Running: {agent_status.get('running', False)}")
        print(f"  - Sent: {agent_status.get('sent_count', 0)}")
        print(f"  - Draft: {agent_status.get('draft_count', 0)}")
        print(f"  - Contact: {agent_status.get('contact_count', 0)}")
        if agent_status.get('last_error'):
            print(f"  - Last Error: {agent_status['last_error']}")

    print("\nAcceptance Criteria:")
    for key, criterion in validation["criteria"].items():
        status_icon = {"PASS": "✅", "FAIL": "❌", "PENDING": "⏳"}[criterion["status"]]
        msg = criterion.get("message", "")
        print(f"  {status_icon} {criterion['name']}: {msg}")

    print(f"\nOverall Status: {validation['overall_status']}")
    print(f"Total Contacts with Replies: {validation['details']['total_sent']}/25")

    if validation["details"]["profiles_missing_raw_text"]:
        print(f"\n⚠️  Profiles missing raw text: {validation['details']['profiles_missing_raw_text']}")

    if validation["details"]["contacts_missing_llm_records"]:
        print(f"\n⚠️  Contacts missing LLM records: {len(validation['details']['contacts_missing_llm_records'])}")
        for item in validation["details"]["contacts_missing_llm_records"][:3]:
            print(f"     - {item['contact_id']}: missing {item['missing_keys']}")

def main():
    """Main monitoring loop."""
    print("Starting tantan agent monitor...")
    print(f"API: {API_BASE}")
    print(f"DB: {DB_PATH}")

    check_interval = 30  # Check every 30 seconds
    max_duration = 3600  # Max 1 hour
    start_time = time.time()

    while time.time() - start_time < max_duration:
        agent_status = get_agent_status()
        validation = validate_acceptance()
        print_status(agent_status, validation)

        # Check if all criteria are met
        if validation["overall_status"] == "PASS":
            print("\n" + "🎉 "*20)
            print("ALL ACCEPTANCE CRITERIA MET!")
            print("🎉 "*20)
            print("\nFinal validation report:")
            print(json.dumps(validation, indent=2))
            return 0

        # Check if agent has completed (no longer running)
        if not agent_status.get("running"):
            print("\n⚠️  Agent stopped")
            if validation["overall_status"] == "FAIL":
                print("Agent stopped but acceptance criteria not met!")
                print(json.dumps(validation, indent=2))
                return 1

        # Wait before next check
        time.sleep(check_interval)

    print("\n⏱️  Monitoring timeout reached")
    validation = validate_acceptance()
    print(json.dumps(validation, indent=2))
    return 1 if validation["overall_status"] != "PASS" else 0

if __name__ == "__main__":
    sys.exit(main())
