"""Supplier-local prepaid ledger. Credit and GPU admission share one transaction."""
import time
import json

from .crypto import canonical


class Credits:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS credit_accounts (buyer TEXT PRIMARY KEY, balance INTEGER NOT NULL CHECK(balance>=0));
        CREATE TABLE IF NOT EXISTS credit_deposits (id TEXT PRIMARY KEY, buyer TEXT NOT NULL, amount INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS credit_holds (
            session TEXT PRIMARY KEY, buyer TEXT NOT NULL, reserved INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS funding_attempts (
            id TEXT PRIMARY KEY, buyer TEXT NOT NULL, created INTEGER NOT NULL,
            response TEXT);
        ''')
        # One supplier daemon owns this database. After a crash, keep recorded
        # compute charges and return every unused reservation to available credit.
        for session, in self.db.execute('SELECT session FROM credit_holds WHERE closed=0').fetchall():
            self.close(session)

    def funding_result(self, quote_hash, buyer):
        row = self.db.execute('SELECT buyer,response FROM funding_attempts WHERE id=?', (quote_hash,)).fetchone()
        if row is None:
            return None
        if row[0] != buyer:
            raise ValueError('Funding buyer mismatch')
        if row[1] is None:
            raise ValueError('Invoice outcome unresolved; this quote cannot create another invoice')
        return json.loads(row[1])

    def begin_funding(self, quote_hash, buyer, limit):
        # Persist BEFORE the wallet call. Timeouts/restarts cannot create another
        # invoice for this quote. Expired quotes cannot reach this journal.
        if not self.store.storage_available():
            raise ValueError('Credit storage unavailable')
        now = int(time.time())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM funding_attempts WHERE created<=?', (now-3600,))
            if self.db.execute('SELECT count(*) FROM funding_attempts').fetchone()[0] >= limit:
                raise ValueError('Provider hourly invoice limit reached')
            self.db.execute('INSERT INTO funding_attempts VALUES (?,?,?,NULL)', (quote_hash,buyer,now))

    def finish_funding(self, quote_hash, response):
        encoded = canonical(response)
        if len(encoded) > 32768:
            raise ValueError('Funding response exceeds storage limit')
        with self.db:
            changed = self.db.execute('UPDATE funding_attempts SET response=? WHERE id=? AND response IS NULL',
                                      (encoded.decode(),quote_hash))
            if changed.rowcount != 1:
                raise ValueError('Invalid funding completion')

    def balance(self, buyer):
        row = self.db.execute('SELECT balance FROM credit_accounts WHERE buyer=?', (buyer,)).fetchone()
        return row[0] if row else 0

    def deposit(self, deposit_id, buyer, amount):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.db.execute('SELECT buyer,amount FROM credit_deposits WHERE id=?', (deposit_id,)).fetchone()
            if old:
                if old != (buyer, amount):
                    raise ValueError('Conflicting credit deposit')
                return self.balance(buyer)
            self.db.execute('INSERT INTO credit_deposits VALUES (?,?,?)', (deposit_id, buyer, amount))
            self.db.execute('INSERT INTO credit_accounts VALUES (?,?) ON CONFLICT(buyer) DO UPDATE SET balance=balance+excluded.balance', (buyer, amount))
        return self.balance(buyer)

    def admit(self, session, buyer, quote, work, request_limit, work_limit):
        if not self.store.storage_available():
            raise ValueError('Provider storage admission limit reached')
        maximum = quote['body']['max_total_msat']
        now = int(time.time())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.balance(buyer) < maximum:
                raise ValueError('Confirmed prepaid credit required before GPU admission')
            self.db.execute('DELETE FROM admission WHERE created<=?', (now-3600,))
            count, used = self.db.execute('SELECT count(*),coalesce(sum(work),0) FROM admission').fetchone()
            if count >= request_limit or used + work > work_limit:
                raise ValueError('Provider hourly work limit reached')
            self.db.execute('INSERT INTO admission VALUES (?,?,?)', (session, now, work))
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,'quoted',?)", (session, buyer, canonical(quote).decode(), now))
            self.db.execute('UPDATE credit_accounts SET balance=balance-? WHERE buyer=?', (maximum, buyer))
            self.db.execute('INSERT INTO credit_holds(session,buyer,reserved,used) VALUES (?,?,?,?)',
                            (session,buyer,maximum,quote['body']['minimum_compute_msat']))

    def charge(self, session, cumulative):
        with self.db:
            changed = self.db.execute('UPDATE credit_holds SET used=? WHERE session=? AND closed=0 AND used<=? AND reserved>=?',
                                      (cumulative,session,cumulative,cumulative))
            if changed.rowcount != 1:
                raise ValueError('Invalid prepaid compute charge')

    def close(self, session):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT buyer,reserved,used,closed FROM credit_holds WHERE session=?', (session,)).fetchone()
            if row and not row[3]:
                self.db.execute('UPDATE credit_accounts SET balance=balance+? WHERE buyer=?', (row[1]-row[2],row[0]))
                self.db.execute('UPDATE credit_holds SET closed=1 WHERE session=?', (session,))

    def status(self, buyer, session=None):
        result = {'balance_msat': self.balance(buyer)}
        if session:
            row = self.db.execute('SELECT reserved,used,closed FROM credit_holds WHERE session=? AND buyer=?', (session,buyer)).fetchone()
            result['session'] = session
            result['hold'] = dict(zip(('reserved_msat','charged_msat','closed'), row)) if row else None
        return result
