"""Durable, atomic USD reservations shared by every process using a ledger.

An uncertain request is never released automatically. Only a complete receipt
can replace a reservation with known usage. This is a local accounting guard,
not a substitute for provider-side account limits or verified model tariffs.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import sqlite3
import uuid


class BudgetError(RuntimeError):
    pass


def _decimal(value: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 50:
        raise BudgetError("Amounts and rates must be decimal strings")
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > Decimal("1e12") or number.as_tuple().exponent < -12:
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        raise BudgetError("Invalid budget amount or token rate") from None


def _integer(value: int, positive: bool = False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= 10**12:
        raise BudgetError("Invalid token count or call limit")
    return value


def _cost(input_tokens: int, output_tokens: int, input_rate: Decimal, output_rate: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        return (input_tokens * input_rate + output_tokens * output_rate) / Decimal(1_000_000)


def _string(value: Decimal) -> str:
    return format(value, "f")


class Budget:
    def __init__(self, path: Path, max_usd: str, max_calls: int):
        self.path = Path(path)
        self.max_usd = _decimal(max_usd)
        self.max_calls = _integer(max_calls, positive=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS limits (id INTEGER PRIMARY KEY CHECK(id=1), max_usd TEXT NOT NULL, max_calls INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL, input_rate TEXT NOT NULL, output_rate TEXT NOT NULL, reserved_usd TEXT NOT NULL, status TEXT NOT NULL, actual_usd TEXT, input_tokens INTEGER, output_tokens INTEGER)")
            row = db.execute("SELECT max_usd,max_calls FROM limits WHERE id=1").fetchone()
            if row is None:
                db.execute("INSERT INTO limits(id,max_usd,max_calls) VALUES(1,?,?)", (_string(self.max_usd), self.max_calls))
            else:
                # Opening an existing ledger never resets totals or silently raises a cap.
                db.execute("UPDATE limits SET max_usd=?,max_calls=? WHERE id=1", (_string(min(Decimal(row[0]), self.max_usd)), min(row[1], self.max_calls)))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _totals(db) -> tuple[Decimal, Decimal, int]:
        spent, reserved = Decimal(0), Decimal(0)
        rows = db.execute("SELECT status,reserved_usd,actual_usd FROM reservations").fetchall()
        with localcontext() as ctx:
            ctx.prec = 80
            for status, held, actual in rows:
                if status == "pending":
                    reserved += Decimal(held)
                else:
                    spent += Decimal(actual)
        return spent, reserved, len(rows)

    def reserve(self, input_bound: int, max_output_tokens: int,
                input_rate: str, output_rate: str) -> str:
        _integer(input_bound)
        _integer(max_output_tokens, positive=True)
        rate_in, rate_out = _decimal(input_rate), _decimal(output_rate)
        cost = _cost(input_bound, max_output_tokens, rate_in, rate_out)
        with self._connect() as db, localcontext() as ctx:
            ctx.prec = 80
            db.execute("BEGIN IMMEDIATE")
            cap, calls_cap, blocked = db.execute("SELECT max_usd,max_calls,blocked FROM limits WHERE id=1").fetchone()
            spent, reserved, calls = self._totals(db)
            if blocked:
                raise BudgetError("Budget ledger is blocked after a usage overrun")
            if calls >= min(calls_cap, self.max_calls):
                raise BudgetError("Cumulative API call limit reached")
            if spent + reserved + cost > min(Decimal(cap), self.max_usd):
                raise BudgetError("Insufficient remaining API budget")
            reservation = uuid.uuid4().hex
            db.execute("INSERT INTO reservations(id,input_bound,output_bound,input_rate,output_rate,reserved_usd,status) VALUES(?,?,?,?,?,?, 'pending')",
                       (reservation, input_bound, max_output_tokens, _string(rate_in), _string(rate_out), _string(cost)))
        return reservation

    def settle(self, reservation_id: str, input_tokens: int, output_tokens: int) -> None:
        _integer(input_tokens)
        _integer(output_tokens)
        overflow = False
        with self._connect() as db, localcontext() as ctx:
            ctx.prec = 80
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT input_bound,output_bound,input_rate,output_rate,status,input_tokens,output_tokens FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise BudgetError("Unknown budget reservation")
            in_bound, out_bound, rate_in, rate_out, status, known_in, known_out = row
            if status != "pending":
                if (known_in, known_out) != (input_tokens, output_tokens):
                    raise BudgetError("Reservation already settled with different usage")
                if status == "overflow":
                    raise BudgetError("Usage exceeded reservation; ledger is blocked")
                return
            actual = _cost(input_tokens, output_tokens, Decimal(rate_in), Decimal(rate_out))
            overflow = input_tokens > in_bound or output_tokens > out_bound
            db.execute("UPDATE reservations SET status=?,actual_usd=?,input_tokens=?,output_tokens=? WHERE id=?",
                       ("overflow" if overflow else "settled", _string(actual), input_tokens, output_tokens, reservation_id))
            spent, reserved, _ = self._totals(db)
            cap = Decimal(db.execute("SELECT max_usd FROM limits WHERE id=1").fetchone()[0])
            if overflow or spent + reserved > cap:
                overflow = True
                db.execute("UPDATE limits SET blocked=1 WHERE id=1")
        # Commit the receipt and permanent block before raising.
        if overflow:
            raise BudgetError("Usage exceeded reservation or current cap; ledger is blocked")

    def snapshot(self) -> dict:
        with self._connect() as db, localcontext() as ctx:
            ctx.prec = 80
            db.execute("BEGIN")
            cap, calls_cap, blocked = db.execute("SELECT max_usd,max_calls,blocked FROM limits WHERE id=1").fetchone()
            spent, reserved, calls = self._totals(db)
            return {"spent_usd": _string(spent), "reserved_usd": _string(reserved),
                    "committed_usd": _string(spent + reserved), "calls": calls,
                    "max_usd": cap, "max_calls": calls_cap, "blocked": bool(blocked)}
