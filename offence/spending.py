"""Conservative durable buyer reservations shared across processes using one data directory."""
import json
import sqlite3
import time


def reserve(directory, session, amount_msat, fee_msat, daily_limit_msat):
    for value in (amount_msat, fee_msat, daily_limit_msat):
        if type(value) is not int or not 0 <= value <= 10**12:
            raise ValueError('Invalid monetary limit')
    path = directory / 'spending.sqlite'
    db = sqlite3.connect(path, timeout=10)
    path.chmod(0o600)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS reservations (session TEXT PRIMARY KEY, created INTEGER, amount INTEGER)')
        if 'complete' not in {r[1] for r in db.execute('PRAGMA table_info(reservations)')}:
            db.execute('ALTER TABLE reservations ADD COLUMN complete INTEGER NOT NULL DEFAULT 0')
        db.execute('BEGIN IMMEDIATE')
        # Active quotes reserve their full ceiling. Stopped sessions retain only
        # dispatched payment exposure; unknown outcomes never expire by time alone.
        now = int(time.time())
        used = db.execute('SELECT coalesce(sum(amount),0) FROM reservations WHERE created>? OR complete=0',
                          (now - 86400,)).fetchone()[0]
        if used + amount_msat + fee_msat > daily_limit_msat:
            raise ValueError('Daily monetary reservation limit exceeded')
        db.execute('INSERT INTO reservations (session,created,amount) VALUES (?,?,?)', (session, now, amount_msat + fee_msat))
        db.commit()
    finally:
        db.close()


def complete(directory, session):
    db = sqlite3.connect(directory / 'spending.sqlite', timeout=10)
    try:
        with db:
            db.execute('UPDATE reservations SET complete=1 WHERE session=?', (session,))
    finally:
        db.close()


def settle(directory, session):
    """Release unused quote capacity, retaining every dispatched payment exposure.

    Only call when this session has stopped. Missing terminal wallet evidence is
    never a failed payment. Fee caps remain conservative until wallet recovery.
    """
    path = directory / 'spending.sqlite'
    if not path.exists():
        return
    amount, uncertain = 0, False
    for attempt_path in directory.glob(session + '.*.attempt.json'):
        attempt = json.loads(attempt_path.read_text())
        name = attempt_path.name.removesuffix('.attempt.json')
        if (directory / (name + '.failed.json')).exists():
            continue
        payment_path = directory / (name + '.payment.json')
        fee = attempt['fee_limit_msat']
        if payment_path.exists():
            payment = json.loads(payment_path.read_text())
            fee = min(payment.get('fee_msat', fee), fee)
        else:
            uncertain = True
        amount += attempt['amount_msat'] + fee
    credit_path = directory / (session + '.credit.json')
    if credit_path.exists():
        credit = json.loads(credit_path.read_text())
        amount += max(0, credit['charged_msat'] - credit['deposited_msat'])
        uncertain = uncertain or credit['pending']
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            # Renew the accounting window on recovery, so a late settlement is
            # not immediately forgotten merely because its quote was old.
            db.execute('UPDATE reservations SET amount=?,complete=?,created=? WHERE session=?',
                       (amount, int(not uncertain), int(time.time()), session))
    finally:
        db.close()


def recover_stopped(directory):
    """Reconcile reservations at exclusive buyer startup or while purchases pause."""
    path = directory / 'spending.sqlite'
    if not path.exists():
        return
    db = sqlite3.connect(path)
    try:
        sessions = [r[0] for r in db.execute('SELECT session FROM reservations WHERE complete=0')]
    finally:
        db.close()
    for session in sessions:
        settle(directory, session)
