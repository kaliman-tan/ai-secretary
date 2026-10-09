"""Supabase → ローカル SQLite 一括移行スクリプト（1回きり）。

使い方（bot を止めてから、WSL2 上で）:
    cd ~/ai-secretary && source .venv/bin/activate
    python scripts/migrate_supabase_to_sqlite.py

- Supabase 側は読み取りのみ。データは一切変更しない
- 取り込み先 DB に既にデータがある場合は何もせず終了する（二重取り込み防止）
- 全テーブルを 1 トランザクションで取り込み、Supabase 側の件数と一致しなければロールバックする
- 個人情報を含むため、行の中身は一切表示しない（件数のみ）
- やり直すときは取り込み先の DB ファイル（data/ai_secretary.db*）を削除してから再実行する

必要: pip install supabase（移行後は不要。requirements.txt からは外してある）
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--db", help="取り込み先 SQLite ファイル（省略時は config.DB_PATH）")
args = parser.parse_args()
if args.db:
    os.environ["DB_PATH"] = args.db  # config の読み込みより前に設定する

from config import DB_PATH, SUPABASE_KEY, SUPABASE_URL  # noqa: E402
from services import db_service  # noqa: E402

try:
    from supabase import create_client
except ImportError:
    sys.exit("supabase パッケージが必要です: pip install supabase")

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("SUPABASE_URL / SUPABASE_KEY が .env にありません（移行が終わるまでは残しておいてください）")

# (テーブル名, 主キー列)
TABLES = [
    ("user_profile", "id"),
    ("custom_rules", "id"),
    ("contacts", "id"),
    ("todos", "id"),
    ("decision_history", "id"),
    ("email_logs", "id"),
    ("research_logs", "id"),
    ("conversation_history", "channel_id"),
    ("event_logs", "id"),
]
TS_COLS = {"created_at", "updated_at", "completed_at"}
PAGE = 1000  # PostgREST は 1 リクエスト 1000 行で打ち切るのでページングが必須


def normalize_ts(value):
    """タイムゾーン付き ISO 文字列を UTC の naive ISO に揃える（新規行の形式と合わせるため）。"""
    if not isinstance(value, str):
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()


def fetch_all(client, table: str, pk: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        batch = client.table(table).select("*").order(pk).range(start, start + PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


def main() -> None:
    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print(f"取り込み先: {DB_PATH}")

    with db_service._conn() as conn:
        for table, _ in TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if n:
                sys.exit(
                    f"中止: 取り込み先の {table} に既に {n} 行あります。二重取り込みを防ぐため何もしていません。\n"
                    f"やり直す場合は {DB_PATH}* を削除してから再実行してください。"
                )

    try:
        with db_service._conn() as conn:  # 例外が出れば全体がロールバックされる
            print(f"{'テーブル':<22}{'Supabase':>10}{'取り込み':>10}")
            for table, pk in TABLES:
                rows = fetch_all(client, table, pk)
                expected = client.table(table).select(pk, count="exact").limit(1).execute().count
                known = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

                dropped: set[str] = set()
                for row in rows:
                    data = {}
                    for k, v in row.items():
                        if k not in known:
                            dropped.add(k)
                            continue
                        data[k] = normalize_ts(v) if k in TS_COLS else v
                    db_service._insert(conn, table, data)

                got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"{table:<22}{expected:>10}{got:>10}")
                if dropped:
                    print(f"  ⚠ SQLite 側に列が無いため捨てた列: {sorted(dropped)}")
                if not (len(rows) == expected == got):
                    raise RuntimeError(f"{table}: 件数不一致（取得 {len(rows)} / Supabase {expected} / 取り込み {got}）")

            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"integrity_check 失敗: {check}")
    except Exception as e:
        sys.exit(f"\n失敗（全体をロールバックしました。取り込み先は空のままです）: {type(e).__name__}: {e}")

    print("\n完了: 全テーブルの件数が一致し、integrity_check も ok でした。")


if __name__ == "__main__":
    main()
