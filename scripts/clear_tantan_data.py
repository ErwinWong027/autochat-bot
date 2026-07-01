from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "social_twin.db"


def main() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("begin immediate")
        conn.execute("create temp table _tantan_threads as select thread_id from threads where platform = 'tantan'")
        conn.execute("delete from thread_pending_groups where thread_id in (select thread_id from _tantan_threads)")
        conn.execute("delete from thread_memory where thread_id in (select thread_id from _tantan_threads)")
        conn.execute("delete from thread_profile_evidence where thread_id in (select thread_id from _tantan_threads)")
        conn.execute("delete from thread_profile_fields where thread_id in (select thread_id from _tantan_threads)")
        conn.execute("delete from thread_messages where thread_id in (select thread_id from _tantan_threads) or platform = 'tantan'")
        conn.execute("delete from threads where platform = 'tantan'")
        conn.execute("delete from messages where channel = 'tantan' or contact_id like 'tantan:%'")
        conn.execute("delete from conversations where channel = 'tantan' or contact_id like 'tantan:%'")
        conn.execute("delete from contact_profile_fields where contact_id like 'tantan:%'")
        conn.execute("delete from profile_evidence where contact_id like 'tantan:%'")
        conn.execute("delete from profile_audits where contact_id like 'tantan:%'")
        conn.execute("delete from draft_cache where contact_id like 'tantan:%'")
        conn.execute("delete from sent_messages where contact_id like 'tantan:%'")
        conn.execute("delete from contacts where contact_id like 'tantan:%'")
        conn.execute("drop table _tantan_threads")
        conn.commit()
    with sqlite3.connect(DB_PATH) as conn:
        threads = conn.execute("select count(*) from threads where platform = 'tantan'").fetchone()[0]
        rows = conn.execute("select count(*) from thread_messages where platform = 'tantan'").fetchone()[0]
    print(f"tantan_threads={threads}")
    print(f"tantan_thread_messages={rows}")
    return 0 if threads == 0 and rows == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
