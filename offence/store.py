"""Local evidence, bounded discovery cache, and recoverable encrypted delivery."""
import json
import sqlite3
import time
from pathlib import Path

from .crypto import canonical, digest, verify
from .models import Advertisement

MAX_ADVERTISEMENT_BYTES = 128 * 1024
MAX_PEER_LIST_BYTES = 256 * 1024


class Store:
    def __init__(self, path: Path, max_peers=512, max_storage_mb=128):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.path = path
        self.max_storage_bytes = max_storage_mb * 1024 * 1024
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        self.db.execute(f"PRAGMA max_page_count={self.max_storage_bytes // page_size}")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, created INTEGER NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS routing_stats (
          provider TEXT, model_id TEXT, latency_ms INTEGER, failures INTEGER NOT NULL,
          cooldown_until INTEGER NOT NULL, updated INTEGER NOT NULL, PRIMARY KEY(provider,model_id));
        CREATE TABLE IF NOT EXISTS admission (
          id TEXT PRIMARY KEY, created INTEGER NOT NULL, work INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS admission_created ON admission(created);
        CREATE TABLE IF NOT EXISTS gateway_budget (
          id TEXT PRIMARY KEY, created INTEGER NOT NULL, tokens INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS peers (
          signer TEXT PRIMARY KEY, sequence INTEGER, expires INTEGER, envelope TEXT);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, buyer TEXT, quote TEXT, state TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS batches (
          session TEXT, seq INTEGER, envelope TEXT, paid INTEGER DEFAULT 0,
          PRIMARY KEY(session,seq));
        CREATE TABLE IF NOT EXISTS receipts (
          id TEXT PRIMARY KEY, session TEXT, seq INTEGER, envelope TEXT,
          UNIQUE(session,seq));
        CREATE TABLE IF NOT EXISTS token_totals (
          model_id TEXT PRIMARY KEY, model_name TEXT, served INTEGER NOT NULL DEFAULT 0,
          paid INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS hosted_batches (
          session TEXT, seq INTEGER, key TEXT NOT NULL, reference TEXT NOT NULL,
          PRIMARY KEY(session,seq));
        CREATE TABLE IF NOT EXISTS observations (
          session TEXT PRIMARY KEY, first_batch_ms INTEGER, elapsed_ms INTEGER);
        """)
        with self.db:
            self.db.execute("UPDATE sessions SET state='interrupted' WHERE state='running'")
            self.db.execute("UPDATE sessions SET state='expired' WHERE state='quoted'")
        self._initialize_token_totals()
        self.max_peers = max_peers
        self.local_signer = None
        self.protected_signers = set()
        for jid, raw in self.db.execute("SELECT id,result FROM jobs").fetchall():
            result = json.loads(raw)
            if result['state'] in {'queued', 'running'}:
                result['state'] = 'interrupted'
                for task in result['tasks']:
                    if task['state'] in {'queued', 'running'}:
                        task['state'] = 'interrupted'
                    for attempt in task.get('attempts',[]):
                        if attempt['state']=='running': attempt['state']='interrupted'
                self.save_job(jid, result)

    def _add_tokens(self, model_id, served=0, paid=0):
        self.db.execute("""INSERT INTO token_totals(model_id,served,paid) VALUES (?,?,?)
            ON CONFLICT(model_id) DO UPDATE SET served=served+excluded.served, paid=paid+excluded.paid""",
            (model_id, served, paid))

    def _initialize_token_totals(self):
        # In-service migration: backfill retained evidence once, then keep aggregates
        # independently of the seven-day detail retention window.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT value FROM meta WHERE key='token_tracker_started'").fetchone():
                return
            for raw, paid, received in self.db.execute("""SELECT b.envelope,b.paid,r.id FROM batches b
                LEFT JOIN receipts r ON r.session=b.session AND r.seq=b.seq""").fetchall():
                header = json.loads(raw)["body"]["sealed"]["header"]
                if paid or received:
                    self._add_tokens(header["model_id"], header["token_count"] if received else 0,
                                     header["token_count"] if paid else 0)
            self.db.execute("INSERT INTO meta VALUES ('token_tracker_started',?)", (int(time.time()),))

    def register_model(self, manifest):
        with self.db:
            self.db.execute("INSERT INTO token_totals(model_id,model_name) VALUES (?,?) ON CONFLICT(model_id) DO UPDATE SET model_name=excluded.model_name",
                            (manifest.model_id, manifest.name))

    def token_totals(self):
        models = [{"model_id": mid, "model_name": name, "served_tokens": served, "paid_tokens": paid}
                  for mid, name, served, paid in self.db.execute(
                      "SELECT model_id,model_name,served,paid FROM token_totals ORDER BY served DESC,model_id")]
        return {"served_tokens": sum(m["served_tokens"] for m in models),
                "paid_tokens": sum(m["paid_tokens"] for m in models), "models": models,
                "tracking_started": self.db.execute(
                    "SELECT value FROM meta WHERE key='token_tracker_started'").fetchone()[0],
                "scope": "this-node", "served_basis": "buyer-receipts",
                "history": "since-tracking-started-plus-retained-earlier-records"}

    def route_stats(self, provider, model_id):
        row = self.db.execute("SELECT latency_ms,failures,cooldown_until FROM routing_stats WHERE provider=? AND model_id=? AND updated>?",
                              (provider,model_id,int(time.time())-7*86400)).fetchone()
        return dict(zip(('latency_ms','failures','cooldown_until'),row)) if row else None

    def record_route(self, provider, model_id, latency_ms, success):
        old = self.route_stats(provider, model_id)
        failures = 0 if success else min(8, (old['failures'] if old else 0) + 1)
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO routing_stats VALUES (?,?,?,?,?,?)", (
                provider, model_id, latency_ms if success else (old['latency_ms'] if old else None),
                failures, 0 if success else now + min(300, 5 * 2**failures), now))
            self.db.execute("DELETE FROM routing_stats WHERE rowid IN (SELECT rowid FROM routing_stats ORDER BY updated DESC LIMIT -1 OFFSET 4096)")

    def job(self, job_id):
        row = self.db.execute("SELECT request_hash,result FROM jobs WHERE id=?", (job_id,)).fetchone()
        return (row[0],json.loads(row[1])) if row else None

    def add_job(self, job_id, request_hash, result, tokens, daily_limit):
        # Reserve and persist together, with a write lock before checking shared quotas.
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE created<?", (int(time.time()) - 7*86400,))
            if self.db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 128:
                raise ValueError("Job retention limit reached")
            if not self.storage_available():
                raise ValueError("Job storage limit reached")
            self.db.execute("DELETE FROM gateway_budget WHERE created<=?",(int(time.time())-86400,))
            used = self.db.execute("SELECT coalesce(sum(tokens),0) FROM gateway_budget").fetchone()[0]
            if used + tokens > daily_limit:
                raise ValueError("Daily reservation limit reached")
            self.db.execute("INSERT INTO gateway_budget VALUES (?,?,?)",(job_id,int(time.time()),tokens))
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job_id, request_hash, int(time.time()), canonical(result).decode()))

    def save_job(self, job_id, result):
        encoded = canonical(result)
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("Job result exceeds storage bound")
        with self.db:
            self.db.execute("UPDATE jobs SET result=? WHERE id=?", (encoded.decode(),job_id))

    def sequence(self):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES ('sequence',1) ON CONFLICT(key) DO UPDATE SET value=value+1")
            return self.db.execute("SELECT value FROM meta WHERE key='sequence'").fetchone()[0]

    def ingest(self, envelope, now=None):
        now = int(time.time()) if now is None else now
        encoded = canonical(envelope)
        if len(encoded) > MAX_ADVERTISEMENT_BYTES:
            raise ValueError("Advertisement exceeds byte limit")
        ad = Advertisement.model_validate(verify(envelope))
        if ad.issued > now + 30 or ad.expires <= now or not 0 < ad.expires - ad.issued <= 300:
            raise ValueError("Expired or invalid advertisement lifetime")
        with self.db:
            self.db.execute("DELETE FROM peers WHERE expires<=?", (now,))
            row = self.db.execute("SELECT sequence FROM peers WHERE signer=?", (envelope["signer"],)).fetchone()
            if row and ad.sequence <= row[0]:
                return False
            if not row and self.db.execute("SELECT count(*) FROM peers").fetchone()[0] >= self.max_peers:
                # Bounded rotation leaves room for newcomers. Pinning is local
                # policy, never a trust claim supplied by gossip.
                protected = self.protected_signers | {self.local_signer}
                victims = [r[0] for r in self.db.execute('SELECT signer FROM peers ORDER BY expires, RANDOM()')
                           if r[0] not in protected]
                if not victims:
                    return False
                self.db.execute('DELETE FROM peers WHERE signer=?', (victims[0],))
            self.db.execute("INSERT OR REPLACE INTO peers VALUES (?,?,?,?)",
                            (envelope["signer"], ad.sequence, ad.expires, encoded.decode()))
        return True

    def peers(self, limit=64, now=None):
        now = int(time.time()) if now is None else now
        result, size = [], 2
        for (raw,) in self.db.execute(
                "SELECT envelope FROM peers WHERE expires>? ORDER BY RANDOM() LIMIT ?", (now, limit)):
            length = len(raw.encode()) + 1
            if size + length > MAX_PEER_LIST_BYTES:
                continue
            result.append(json.loads(raw))
            size += length
        return result

    def peer_count(self, excluded_signer):
        return self.db.execute("SELECT count(*) FROM peers WHERE expires>? AND signer!=?",
                               (int(time.time()), excluded_signer)).fetchone()[0]

    def storage_available(self):
        # WAL and database both consume the provider's volume. Fail closed before work.
        return sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal"))
                   if p.exists()) < self.max_storage_bytes * 3 // 4

    def admit(self, session_id, work, request_limit, work_limit, buyer=None, quote=None):
        if not self.storage_available():
            raise ValueError("Provider storage admission limit reached")
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM admission WHERE created<=?", (now - 3600,))
            count, used = self.db.execute("SELECT count(*),coalesce(sum(work),0) FROM admission").fetchone()
            if count >= request_limit or used + work > work_limit:
                raise ValueError("Provider hourly work limit reached")
            self.db.execute("INSERT INTO admission VALUES (?,?,?)", (session_id, now, work))
            if quote is not None:
                self.db.execute("INSERT INTO sessions VALUES (?,?,?,'quoted',?)",
                                (session_id, buyer, canonical(quote).decode(), now))

    def reserve_gateway(self, request_id, tokens, limit):
        if not self.storage_available():
            raise ValueError("Gateway storage limit reached")
        # Rolling 24h reservations survive restart and are deliberately not refunded
        # on ambiguous disconnects. No automatic retry can bypass this quota.
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM gateway_budget WHERE created<=?", (now - 86400,))
            used = self.db.execute("SELECT coalesce(sum(tokens),0) FROM gateway_budget").fetchone()[0]
            if used + tokens > limit:
                raise ValueError("Gateway daily output reservation limit reached")
            self.db.execute("INSERT INTO gateway_budget VALUES (?,?,?)", (request_id, now, tokens))

    def session(self, session_id, buyer, quote):
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,'quoted',?)",
                            (session_id, buyer, canonical(quote).decode(), int(time.time())))

    def state(self, session_id, state):
        with self.db:
            self.db.execute("UPDATE sessions SET state=? WHERE id=?", (state, session_id))

    def batch(self, session_id, seq, envelope, hosted=None):
        with self.db:
            self.db.execute("INSERT INTO batches(session,seq,envelope) VALUES (?,?,?)",
                            (session_id, seq, canonical(envelope).decode()))
            if hosted:
                self.db.execute("INSERT INTO hosted_batches VALUES (?,?,?,?)",
                    (session_id, seq, hosted["key"], canonical(hosted["reference"]).decode()))

    def hosted_batch(self, session, seq):
        row = self.db.execute("SELECT key,reference FROM hosted_batches WHERE session=? AND seq=?", (session,seq)).fetchone()
        if not row:
            raise ValueError("Unknown hosted batch")
        return {"key":row[0], "reference":json.loads(row[1])}

    def paid(self, session_id, seq):
        with self.db:
            changed = self.db.execute("UPDATE batches SET paid=1 WHERE session=? AND seq=? AND paid=0", (session_id, seq))
            if changed.rowcount:
                raw = self.db.execute("SELECT envelope FROM batches WHERE session=? AND seq=?", (session_id, seq)).fetchone()[0]
                header = json.loads(raw)["body"]["sealed"]["header"]
                if header["amount_msat"] <= 0:
                    # A prepaid fractional stream can have zero-increment chunks.
                    # They are paid delivery only when backed by a real credit hold.
                    batch = json.loads(raw)['body']
                    prepaid = batch.get('settlement_mode') == 'prepaid-v1'
                    held = self.db.execute('SELECT used FROM credit_holds WHERE session=?', (session_id,)).fetchone() if prepaid else None
                    if not held or held[0] <= 0:
                        raise ValueError("Free output has no payment settlement")
                self._add_tokens(header["model_id"], paid=header["token_count"])

    def unsettled_batches(self, limit=32, offset=0):
        return [(session, seq, json.loads(raw)) for session, seq, raw in self.db.execute(
            "SELECT session,seq,envelope FROM batches WHERE paid=0 AND json_extract(envelope,'$.body.sealed.header.amount_msat')>0 ORDER BY session,seq LIMIT ? OFFSET ?", (limit, offset))]

    def recover(self, session_id, buyer, offset=None):
        row = self.db.execute("SELECT buyer,quote,state FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row or row[0] != buyer:
            raise ValueError("Unknown session")
        legacy = offset is None
        if legacy:
            offset = 0
        if type(offset) is not int or not 0 <= offset <= 32768:
            raise ValueError('Invalid recovery offset')
        batches, size, more = [], 0, False
        for seq, raw in self.db.execute('SELECT seq,envelope FROM batches WHERE session=? AND seq>=? ORDER BY seq LIMIT 65',
                                       (session_id,offset)):
            length = len(raw.encode())
            if size+length > 256*1024 or len(batches) == 64:
                more = True
                break
            batches.append(json.loads(raw))
            size += length
        if more and not batches:
            raise ValueError('Stored batch exceeds recovery page limit')
        if more and legacy:
            raise ValueError('Recovery requires offset pagination')
        return {'quote':json.loads(row[1]), 'state':row[2], 'batches':batches,
                'next_offset':offset+len(batches) if more else None}

    def receipt(self, envelope):
        body = verify(envelope)
        expected_keys = {"type", "session", "sequence", "batch_hash", "payment_hash", "received_tokens"}
        if set(body) != expected_keys or body["type"] != "receipt":
            raise ValueError("Invalid receipt")
        row = self.db.execute("SELECT buyer FROM sessions WHERE id=?", (body["session"],)).fetchone()
        if not row or row[0] != envelope["signer"]:
            raise ValueError("Receipt buyer mismatch")
        row = self.db.execute("SELECT envelope FROM batches WHERE session=? AND seq=?",
                              (body["session"], body["sequence"])).fetchone()
        if not row:
            raise ValueError("Unknown batch")
        batch = json.loads(row[0])
        data = verify(batch)
        if (body["batch_hash"] != digest(batch) or body["payment_hash"] != data.get("invoice_payment_hash", data["sealed"]["payment_hash"])
                or type(body["received_tokens"]) is not int
                or body["received_tokens"] != data["sealed"]["header"]["token_count"]):
            raise ValueError("Receipt does not bind delivered batch")
        with self.db:
            previous = self.db.execute("SELECT id FROM receipts WHERE session=? AND seq=?",
                                      (body["session"], body["sequence"])).fetchone()
            if previous and previous[0] != digest(envelope):
                raise ValueError("Conflicting receipt")
            inserted = self.db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)",
                            (digest(envelope), body["session"], body["sequence"], canonical(envelope).decode()))
            if inserted.rowcount:
                self._add_tokens(data["sealed"]["header"]["model_id"], served=body["received_tokens"])

    def evidence(self):
        # This is local observation, not a global score or proof against Sybils.
        states = dict(self.db.execute("SELECT state,count(*) FROM sessions GROUP BY state"))
        paid = self.db.execute("SELECT count(*) FROM batches WHERE paid=1").fetchone()[0]
        receipts = self.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
        samples = [r[0] for r in self.db.execute("SELECT first_batch_ms FROM observations WHERE first_batch_ms IS NOT NULL")]
        return {"scope": "provider-local-claims", "window_days": 7, "sessions": states, "settled_batches": paid,
                "buyer_receipts": receipts, "execution_verified": False, "latency_samples": len(samples),
                "mean_first_batch_ms": sum(samples) // len(samples) if samples else None}

    def observe(self, session, first_batch_ms, elapsed_ms):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?)", (session, first_batch_ms, elapsed_ms))

    def evidence_packages(self, session=None, limit=16):
        where, args = ("WHERE r.session=?", (session, limit)) if session else ("", (limit,))
        query = f"""SELECT s.quote,b.envelope,r.envelope FROM receipts r
          JOIN sessions s ON s.id=r.session JOIN batches b ON b.session=r.session AND b.seq=r.seq
          {where} ORDER BY s.created DESC LIMIT ?"""
        return [{"quote": json.loads(q), "batch": json.loads(b), "receipt": json.loads(r)}
                for q, b, r in self.db.execute(query, args)]

    def cleanup(self):
        cutoff = int(time.time()) - 7 * 86400
        with self.db:
            for table in ("receipts", "batches", "observations"):
                self.db.execute(f"DELETE FROM {table} WHERE session IN (SELECT id FROM sessions WHERE created<? AND id NOT IN (SELECT session FROM hosted_batches))", (cutoff,))
            self.db.execute("DELETE FROM sessions WHERE created<? AND id NOT IN (SELECT session FROM hosted_batches)", (cutoff,))

    def close(self):
        self.db.close()
