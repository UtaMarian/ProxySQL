"""Thin client for the ProxySQL admin interface (MySQL protocol, SQLite dialect).

ProxySQL's admin interface is backed by SQLite, so values are quoted with SQLite
rules ('' for a single quote, backslash is literal) instead of the MySQL escaping
done by client libraries.
"""
import re

import pymysql

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


class ProxySQLError(Exception):
    pass


def lit(value):
    """Render a Python value as a SQLite literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    s = str(value)
    if "\x00" in s:
        raise ValueError("NUL bytes are not allowed")
    return "'" + s.replace("'", "''") + "'"


def ident(name):
    if not _IDENT_RE.match(name or ""):
        raise ValueError("Invalid identifier: %r" % name)
    return name


def _norm(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


class AdminClient(object):
    def __init__(self, instance):
        self.instance = instance
        self.conn = None

    def __enter__(self):
        try:
            self.conn = pymysql.connect(
                host=self.instance.host,
                port=self.instance.port,
                user=self.instance.user,
                password=self.instance.password,
                connect_timeout=self.instance.connect_timeout,
                read_timeout=self.instance.read_timeout,
                write_timeout=self.instance.read_timeout,
                autocommit=True,
                charset="utf8mb4",
            )
        except pymysql.MySQLError as e:
            raise ProxySQLError("Cannot connect to ProxySQL admin %s:%s - %s"
                                % (self.instance.host, self.instance.port, _errmsg(e)))
        return self

    def __exit__(self, *exc):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass

    def query(self, sql):
        """Run a statement; returns (columns, rows-as-dicts)."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return [], []
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, [_norm(v) for v in r])) for r in cur.fetchall()]
                return cols, rows
        except pymysql.MySQLError as e:
            raise ProxySQLError(_errmsg(e))

    def rows(self, sql):
        return self.query(sql)[1]

    def execute(self, sql):
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                return cur.rowcount
        except pymysql.MySQLError as e:
            raise ProxySQLError(_errmsg(e))

    def columns(self, table):
        cols, _ = self.query("SELECT * FROM %s LIMIT 0" % ident(table))
        return cols

    def raw(self, sql):
        """Run an arbitrary statement (SQL console)."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return {"columns": [], "rows": [], "affected": cur.rowcount}
                cols = [d[0] for d in cur.description]
                rows = [[_norm(v) for v in r] for r in cur.fetchall()]
                return {"columns": cols, "rows": rows, "affected": None}
        except pymysql.MySQLError as e:
            raise ProxySQLError(_errmsg(e))


def _errmsg(e):
    if getattr(e, "args", None) and len(e.args) >= 2:
        return "(%s) %s" % (e.args[0], e.args[1])
    return str(e)
