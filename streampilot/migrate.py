# SPDX-License-Identifier: LGPL-2.1-or-later
"""Copy an existing SQLite database (mini.db) into a Postgres database.

    python -m streampilot.migrate --from mini.db --to postgresql://user:pass@host/dbname

* Stop StreamPilot first: the source is read, never modified, but new data
  written during the copy would be missed.
* The target schema is created with StreamPilot's own start-up code, so it is
  exactly what a fresh Postgres install gets.
* The whole copy runs in ONE transaction: any error leaves the target data
  untouched. Row ids are preserved and the id sequences are advanced afterwards,
  so new rows never collide with migrated ones.
* The target must be empty unless --force is given (which truncates the target
  tables first).
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Parents first (live_sample references live_session). Anything not listed is
# reported and skipped.
TABLE_ORDER = [
    "devices", "slack_config", "device_slack", "srt_gw_slack", "device_output_map",
    "srt_gateway_device", "srt_endpoint_location",
    "live_session", "live_sample", "streamhub_log",
    "srt_route_sample", "srt_connection_sample", "srt_session", "srt_event", "srt_report_session",
]
# Tables that _init_db() seeds with a default row: that row is not "user data".
SEEDED = {"slack_config": "id"}


def _mask(url: str) -> str:
    u = urlsplit(url)
    return f"{u.hostname or 'localhost'}:{u.port or 5432}{u.path}"


def _source_path(arg: str) -> Path:
    if arg.lower().startswith("sqlite:///"):
        arg = arg[len("sqlite:///"):]
    p = Path(arg)
    if not p.is_file():
        sys.exit(f"error: source database not found: {p}")
    return p


def _clean(v):
    """PostgreSQL text cannot hold NUL; SQLite can. Returns (value, was_changed)."""
    if isinstance(v, str) and "\x00" in v:
        return v.replace("\x00", ""), True
    return v, False


def migrate(src_path: Path, to_url: str, force: bool = False, batch_size: int = 5000, out=print) -> dict:
    """Run the migration. Returns {table: (source_rows, target_rows)}; raises on any failure."""
    import psycopg
    from psycopg import sql

    scheme = to_url.split(":", 1)[0].lower()
    if scheme not in ("postgres", "postgresql"):
        raise SystemExit("error: --to must be a postgresql:// URL")

    # Create the schema with the app's own code (it reads the URL from the environment).
    os.environ["STREAMPILOT_DB_URL"] = to_url
    from . import db, server  # noqa: F401  (import creates the live_* tables, _init_db the rest)
    server._init_db()
    db.close_pool()

    src = sqlite3.connect(str(src_path))
    src.execute("BEGIN")  # one consistent read snapshot
    src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    skipped = sorted(t for t in src_tables if t not in TABLE_ORDER and not t.startswith("sqlite_"))
    if skipped:
        out(f"note: skipping tables not known to StreamPilot: {', '.join(skipped)}")
    tables = [t for t in TABLE_ORDER if t in src_tables]

    result = {}
    with psycopg.connect(to_url) as pg:  # default (non-autocommit): one transaction
        # ---- target must be empty (ignoring seeded default rows)
        for t in tables:
            seeded = SEEDED.get(t)
            where = sql.SQL("") if not seeded else sql.SQL(" WHERE false")
            n = pg.execute(sql.SQL("SELECT COUNT(*) FROM {}{}").format(sql.Identifier(t), where)).fetchone()[0]
            if n and not force:
                raise SystemExit(f"error: target table {t!r} already has {n} rows; "
                                 f"use --force to truncate the target tables first")
        if force:
            names = sql.SQL(", ").join(sql.Identifier(t) for t in tables)
            pg.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(names))
            out(f"--force: truncated {len(tables)} target tables")

        nul_fixed = 0
        for t in tables:
            tgt_cols = {r[0].lower() for r in pg.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s", (t,))}
            src_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")')]
            cols = [c for c in src_cols if c.lower() in tgt_cols]
            dropped = [c for c in src_cols if c.lower() not in tgt_cols]
            if dropped:
                out(f"note: {t}: source columns not in target are ignored: {', '.join(dropped)}")
            col_sql = sql.SQL(", ").join(sql.Identifier(c.lower()) for c in cols)
            if t in SEEDED:  # a default row exists already: replace it with the real one
                pg.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(t)))

            copied = 0
            try:
                with pg.cursor() as cur, cur.copy(
                    sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(t), col_sql)
                ) as copy:
                    sel = src.execute("SELECT " + ", ".join(f'"{c}"' for c in cols) + f' FROM "{t}"')
                    while True:
                        rows = sel.fetchmany(batch_size)
                        if not rows:
                            break
                        for row in rows:
                            fixed = [_clean(v) for v in row]
                            nul_fixed += sum(1 for _, ch in fixed if ch)
                            copy.write_row([v for v, _ in fixed])
                        copied += len(rows)
            except Exception as e:
                raise SystemExit(f"error: copying table {t!r} failed after {copied} rows: {e}\n"
                                 f"(nothing was committed; the target data is unchanged)")
            out(f"  {t:<24} {copied:>10} rows")

        # ---- advance id sequences so new inserts never collide with copied ids
        for t in tables:
            if "id" not in {r[0] for r in pg.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s", (t,))}:
                continue
            seq = pg.execute("SELECT pg_get_serial_sequence(%s, 'id')", (t,)).fetchone()[0]
            if seq:
                pg.execute(sql.SQL("SELECT setval(%s, COALESCE((SELECT MAX(id) FROM {}), 1), "
                                   "(SELECT MAX(id) IS NOT NULL FROM {}))").format(
                    sql.Identifier(t), sql.Identifier(t)), (seq,))

        # ---- verify (inside the transaction, so a mismatch rolls everything back)
        bad = []
        for t in tables:
            s_n = src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            t_n = pg.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
            result[t] = (s_n, t_n)
            if s_n != t_n:
                bad.append(f"{t}: source {s_n} != target {t_n}")
        if bad:
            raise SystemExit("error: row counts differ, rolling back:\n  " + "\n  ".join(bad))
        if nul_fixed:
            out(f"note: removed NUL characters from {nul_fixed} text value(s) (not storable in PostgreSQL)")
    src.close()
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m streampilot.migrate",
                                 description="Copy a StreamPilot SQLite database into Postgres.")
    ap.add_argument("--from", dest="src", required=True, help="SQLite file (or sqlite:///path)")
    ap.add_argument("--to", dest="dst", required=True, help="postgresql://user:pass@host/dbname (must exist)")
    ap.add_argument("--force", action="store_true", help="truncate the target tables first")
    ap.add_argument("--batch-size", type=int, default=5000)
    a = ap.parse_args(argv)

    src = _source_path(a.src)
    print(f"Migrating {src} -> {_mask(a.dst)}")
    print("(stop StreamPilot first so no new data is written during the copy)")
    res = migrate(src, a.dst, force=a.force, batch_size=a.batch_size)
    total = sum(n for n, _ in res.values())
    print(f"OK: {total} rows in {len(res)} tables copied and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
