"""Command line management of web UI accounts.

    proxysql-webui-admin create-user NAME --role admin [--password PW]
    proxysql-webui-admin set-password NAME
    proxysql-webui-admin list-users
    proxysql-webui-admin delete-user NAME
"""
import argparse
import getpass
import sys
import time

from . import db
from .auth import hash_password, validate_new_password
from .config import Config


def _password(args):
    if args.password:
        return args.password
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return pw


def main(argv=None):
    p = argparse.ArgumentParser(prog="proxysql-webui-admin")
    p.add_argument("--config", help="Path to config.ini")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--role", choices=db.ROLES, default="viewer")
    c.add_argument("--password")
    s = sub.add_parser("set-password")
    s.add_argument("username")
    s.add_argument("--password")
    r = sub.add_parser("set-role")
    r.add_argument("username")
    r.add_argument("role", choices=db.ROLES)
    sub.add_parser("list-users")
    d = sub.add_parser("delete-user")
    d.add_argument("username")
    u = sub.add_parser("unlock", help="Clear login failure counters")
    u.add_argument("username", nargs="?")
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 2

    cfg = Config(args.config)
    db.init_db(cfg.db_path)
    conn = db.connect(cfg.db_path)

    if args.cmd == "create-user":
        pw = _password(args)
        try:
            validate_new_password(pw)
        except ValueError as e:
            sys.exit(str(e))
        try:
            conn.execute("INSERT INTO users (username, password_hash, role, active, created_at) "
                         "VALUES (?,?,?,1,?)", (args.username, hash_password(pw), args.role, int(time.time())))
        except Exception:
            sys.exit("User %s already exists" % args.username)
        print("Created %s (%s)" % (args.username, args.role))
    elif args.cmd == "set-password":
        pw = _password(args)
        try:
            validate_new_password(pw)
        except ValueError as e:
            sys.exit(str(e))
        n = conn.execute("UPDATE users SET password_hash=?, active=1 WHERE username=?",
                         (hash_password(pw), args.username)).rowcount
        if not n:
            sys.exit("No such user")
        print("Password updated")
    elif args.cmd == "set-role":
        if not conn.execute("UPDATE users SET role=? WHERE username=?", (args.role, args.username)).rowcount:
            sys.exit("No such user")
        print("Role updated")
    elif args.cmd == "list-users":
        for r in conn.execute("SELECT username, role, active, last_login FROM users ORDER BY username"):
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_login"])) if r["last_login"] else "never"
            print("%-24s %-9s %-8s last login: %s" % (r["username"], r["role"],
                                                     "active" if r["active"] else "disabled", last))
    elif args.cmd == "delete-user":
        if not conn.execute("DELETE FROM users WHERE username=?", (args.username,)).rowcount:
            sys.exit("No such user")
        print("Deleted")
    elif args.cmd == "unlock":
        if args.username:
            conn.execute("DELETE FROM login_failures WHERE username=?", (args.username,))
        else:
            conn.execute("DELETE FROM login_failures")
        print("Login failure counters cleared")
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
