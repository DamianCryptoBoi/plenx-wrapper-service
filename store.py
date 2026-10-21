"""Private server-side account credentials and persistent job metadata."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from hashlib import sha256
from uuid import uuid4

TERMINAL = ("completed", "succeeded", "success", "failed", "error", "cancelled", "canceled")
GENERATION_LIMIT = 10


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT,
                kind TEXT NOT NULL, prompt TEXT NOT NULL,
                parameters TEXT NOT NULL, status TEXT NOT NULL,
                response TEXT, error TEXT, poll_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created_at DESC);
            CREATE INDEX IF NOT EXISTS jobs_task ON jobs(owner, kind, task_id);
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(owner, kind, status);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='submission_unknown', error=? WHERE status='submitting' AND task_id IS NULL",
                ("Service stopped during submission. Check Plenx before retrying.",),
            )

    @staticmethod
    def public_account(row) -> dict:
        return {key: row[key] for key in ("id", "name", "created_at", "updated_at")} | {"has_token": row["token"] is not None}

    def accounts(self) -> list[dict]:
        return [self.public_account(row) for row in self.db.execute("SELECT * FROM accounts WHERE token IS NOT NULL ORDER BY created_at,id")]

    def account(self, account_id: str | None = None):
        if account_id:
            row = self.db.execute("SELECT * FROM accounts WHERE id=? AND token IS NOT NULL", (account_id,)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM accounts WHERE token IS NOT NULL ORDER BY created_at,id LIMIT 1").fetchone()
        return dict(row) if row else None

    def save_account(self, name: str, token: str | None, account_id: str | None = None) -> dict:
        timestamp = now()
        fingerprint = sha256(token.encode()).hexdigest() if token is not None else None
        with self.db:
            if account_id:
                if token is None:
                    self.db.execute("UPDATE accounts SET name=?,updated_at=? WHERE id=?", (name, timestamp, account_id))
                else:
                    self.db.execute("UPDATE accounts SET name=?,token=?,token_hash=?,updated_at=? WHERE id=?", (name, token, fingerprint, timestamp, account_id))
            else:
                existing = self.db.execute("SELECT * FROM accounts WHERE token_hash=?", (fingerprint,)).fetchone()
                if existing:
                    account_id = existing["id"]
                    if existing["token"] is None:
                        self.db.execute("UPDATE accounts SET name=?,token=?,updated_at=? WHERE id=?", (name, token, timestamp, account_id))
                else:
                    account_id = str(uuid4())
                    self.db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?)", (account_id, name, token, fingerprint, timestamp, timestamp))
            if fingerprint:
                # Adopt history created before credentials moved out of localStorage.
                self.db.execute("UPDATE jobs SET owner=? WHERE owner=?", (account_id, fingerprint))
        return self.public_account(self.account(account_id))

    def remove_account(self, account_id: str):
        with self.db:
            # Keep its ID/fingerprint so re-adding the same credential restores history.
            self.db.execute("UPDATE accounts SET token=NULL,updated_at=? WHERE id=?", (now(), account_id))
            self.db.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=? WHERE owner=? AND status='waiting' AND task_id IS NULL",
                            ("Account removed before submission.", now(), account_id))

    def has_capacity(self, owner: str, kind: str) -> bool:
        # Unknown submissions still reserve a slot: Plenx may have accepted them.
        terminal = ",".join("?" for _ in TERMINAL)
        count = self.db.execute(f"SELECT COUNT(*) FROM jobs WHERE owner=? AND kind=? AND status NOT IN ({terminal}) AND NOT (status='waiting' AND task_id IS NULL)",
                                (owner, kind, *TERMINAL)).fetchone()[0]
        return count < GENERATION_LIMIT

    def create(self, owner: str, kind: str, parameters: dict) -> str:
        job_id, timestamp = str(uuid4()), now()
        prompt = parameters["prompt"] if kind == "video" else parameters["payload"]["prompt"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            waiting = self.db.execute("SELECT 1 FROM jobs WHERE owner=? AND kind=? AND status='waiting' AND task_id IS NULL LIMIT 1", (owner, kind)).fetchone()
            status = "submitting" if self.has_capacity(owner, kind) and not waiting else "waiting"
            self.db.execute(
                "INSERT INTO jobs (id,owner,kind,prompt,parameters,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, owner, kind, prompt, json.dumps(parameters), status, timestamp, timestamp),
            )
        return job_id

    def claim_waiting(self, owner: str, kind: str):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.has_capacity(owner, kind):
                return None
            row = self.db.execute("SELECT id FROM jobs WHERE owner=? AND kind=? AND status='waiting' AND task_id IS NULL ORDER BY rowid LIMIT 1", (owner, kind)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET status='submitting',updated_at=? WHERE id=?", (now(), row["id"]))
        return self.get(row["id"])

    def decode_job(self, row):
        job = dict(row)
        job["account_id"] = job.pop("owner")
        job["parameters"] = json.loads(job["parameters"])
        job["response"] = json.loads(job["response"]) if job["response"] else None
        job["queue_position"] = None
        if job["status"] == "waiting" and job["task_id"] is None:
            job["queue_position"] = self.db.execute("""
                SELECT COUNT(*) FROM jobs WHERE owner=? AND kind=? AND status='waiting' AND task_id IS NULL
                  AND rowid <= (SELECT rowid FROM jobs WHERE id=?)
            """, (job["account_id"], job["kind"], job["id"])).fetchone()[0]
        return job

    def get(self, job_id: str, account_id: str | None = None):
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND (? IS NULL OR owner=?)", (job_id, account_id, account_id)).fetchone()
        return self.decode_job(row) if row else None

    def in_flight(self):
        terminal = ",".join("?" for _ in TERMINAL)
        rows = self.db.execute(f"""
            SELECT jobs.* FROM jobs JOIN accounts ON accounts.id=jobs.owner
            WHERE accounts.token IS NOT NULL AND jobs.task_id IS NOT NULL
              AND jobs.status NOT IN ({terminal}) ORDER BY jobs.rowid
        """, TERMINAL).fetchall()
        return [self.decode_job(row) for row in rows]

    def update(self, job_id: str, response: dict):
        with self.db:
            terminal = ",".join("?" for _ in TERMINAL)
            self.db.execute(
                f"UPDATE jobs SET task_id=?,status=?,response=?,error=NULL,poll_error=NULL,updated_at=? WHERE id=? AND status NOT IN ({terminal})",
                (response["task_id"], response["status"].lower(), json.dumps(response), now(), job_id, *TERMINAL),
            )

    def fail(self, job_id: str, status: str, error: str):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, now(), job_id))

    def poll_error(self, job_id: str, error: str):
        with self.db:
            terminal = ",".join("?" for _ in TERMINAL)
            self.db.execute(f"UPDATE jobs SET poll_error=?,updated_at=? WHERE id=? AND status NOT IN ({terminal})", (error, now(), job_id, *TERMINAL))

    def find(self, account_id: str | None, kind: str, task_id: str):
        rows = self.db.execute("""
            SELECT jobs.id,jobs.owner FROM jobs JOIN accounts ON accounts.id=jobs.owner
            WHERE accounts.token IS NOT NULL AND jobs.kind=? AND jobs.task_id=?
              AND (? IS NULL OR jobs.owner=?) ORDER BY jobs.created_at DESC
        """, (kind, task_id, account_id, account_id)).fetchall()
        if len({row["owner"] for row in rows}) > 1:
            raise ValueError("Task ID belongs to multiple accounts. Supply X-Account-ID.")
        return rows[0] if rows else None

    def list(self, owner: str | None = None) -> list[dict]:
        if owner is None:
            jobs = [job for account in self.accounts() for job in self.list(account["id"])]
            return sorted(jobs, key=lambda job: job["created_at"], reverse=True)
        # ponytail: keep all current jobs, cap terminal history at 200; paginate if needed.
        placeholders = ",".join("?" for _ in TERMINAL)
        rows = self.db.execute(f"""
            WITH recent_terminal AS (
                SELECT id FROM jobs WHERE owner=? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 200
            )
            SELECT * FROM jobs WHERE owner=? AND (
                status NOT IN ({placeholders}) OR id IN (SELECT id FROM recent_terminal)
            ) ORDER BY created_at DESC
        """, (owner, *TERMINAL, owner, *TERMINAL)).fetchall()
        return [self.decode_job(row) for row in rows]

    def close(self):
        self.db.close()
