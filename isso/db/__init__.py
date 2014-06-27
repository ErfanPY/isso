# -*- encoding: utf-8 -*-

from __future__ import unicode_literals

import logging
import sqlite3
import operator
import threading

import os.path

from collections import defaultdict

logger = logging.getLogger("isso")

from isso.db.comments import Comments
from isso.db.threads import Threads
from isso.db.spam import Guard
from isso.db.preferences import Preferences


class Transaction(object):
    """A context manager to lock the database across processes and automatic
    rollback on failure. On success, reset the isolation level back to normal.

    SQLite3's DEFERRED (default) transaction mode causes database corruption
    for concurrent writes to the database from multiple processes. IMMEDIATE
    ensures a global write lock, but reading is still possible.
    """

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        self._orig = self.con.isolation_level
        self.con.isolation_level = "IMMEDIATE"
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type:
                self.con.rollback()
            else:
                self.con.commit()
        finally:
            self.con.isolation_level = self._orig


class SQLite3(object):
    """SQLite3 connection pool across multiple threads. Implementation idea
    from `Peewee <https://github.com/coleifer/peewee>`_.
    """

    def __init__(self, db):
        self.db = os.path.expanduser(db)
        self.lock = threading.Lock()
        self.local = threading.local()

    def connect(self):
        with self.lock:
            self.local.conn = sqlite3.connect(self.db, isolation_level=None)

    def close(self):
        with self.lock:
            self.local.conn.close()
            self.local.conn = None

    def execute(self, sql, args=()):
        if isinstance(sql, (list, tuple)):
            sql = ' '.join(sql)

        return self.connection.execute(sql, args)

    @property
    def connection(self):
        if not hasattr(self.local, 'conn') or self.local.conn is None:
            self.connect()
        return self.local.conn

    @property
    def transaction(self):
        return Transaction(self.connection)

    @property
    def total_changes(self):
        return self.connection.total_changes


class Adapter(object):
    """DB-dependend wrapper around SQLite3.

    Runs migration if `user_version` is older than `MAX_VERSION` and register
    a trigger for automated orphan removal.
    """

    MAX_VERSION = 3

    def __init__(self, conn, conf):
        self.connection = conn
        self.conf = conf

        rv = self.execute([
            "SELECT name FROM sqlite_master"
            "   WHERE type='table' AND name IN ('threads', 'comments', 'preferences')"]
        ).fetchone()

        self.preferences = Preferences(self)
        self.threads = Threads(self)
        self.comments = Comments(self)
        self.guard = Guard(self)

        if rv is None:
            self.execute("PRAGMA user_version = %i" % Adapter.MAX_VERSION)
        else:
            self.migrate(to=Adapter.MAX_VERSION)

        self.execute([
            'CREATE TRIGGER IF NOT EXISTS remove_stale_threads',
            'AFTER DELETE ON comments',
            'BEGIN',
            '    DELETE FROM threads WHERE id NOT IN (SELECT tid FROM comments);',
            'END'])

    @property
    def version(self):
        return self.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self, to):

        if self.version >= to:
            return

        logger.info("migrate database from version %i to %i", self.version, to)

        # re-initialize voters blob due a bug in the bloomfilter signature
        # which added older commenter's ip addresses to the current voters blob
        if self.version == 0:

            from isso.utils import Bloomfilter
            bf = buffer(Bloomfilter(iterable=["127.0.0.0"]).array)

            with self.connection.transaction as con:
                con.execute('UPDATE comments SET voters=?', (bf, ))
                con.execute('PRAGMA user_version = 1')
                logger.info("%i rows changed", con.total_changes)

        # move [general] session-key to database
        if self.version == 1:

            with self.connection.transaction as con:
                if self.conf.has_option("general", "session-key"):
                    con.execute('UPDATE preferences SET value=? WHERE key=?', (
                        self.conf.get("general", "session-key"), "session-key"))

                con.execute('PRAGMA user_version = 2')
                logger.info("%i rows changed", con.total_changes)

        # limit max. nesting level to 1
        if self.version == 2:

            first = lambda rv: list(map(operator.itemgetter(0), rv))

            with self.connection.transaction as con:
                top = first(con.execute("SELECT id FROM comments WHERE parent IS NULL").fetchall())
                flattened = defaultdict(set)

                for id in top:

                    ids = [id, ]

                    while ids:
                        rv = first(con.execute("SELECT id FROM comments WHERE parent=?", (ids.pop(), )))
                        ids.extend(rv)
                        flattened[id].update(set(rv))

                for id in flattened.keys():
                    for n in flattened[id]:
                        con.execute("UPDATE comments SET parent=? WHERE id=?", (id, n))

                con.execute('PRAGMA user_version = 3')
                logger.info("%i rows changed", con.total_changes)

    def execute(self, sql, args=()):
        return self.connection.execute(sql, args)
