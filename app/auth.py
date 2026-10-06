"""Session authentication, roles and CSRF protection."""
import functools
import hashlib
import hmac
import secrets
import time

from flask import Blueprint, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import ROLES, audit, get_db

bp = Blueprint("auth", __name__)

# pbkdf2:sha256 keeps hashing FIPS-compatible on RHEL hosts running in FIPS mode
HASH_METHOD = "pbkdf2:sha256:600000"
_DUMMY_HASH = generate_password_hash("dummy-password", method=HASH_METHOD)


def hash_password(pw):
    return generate_password_hash(pw, method=HASH_METHOD)


def validate_new_password(pw):
    if not pw or len(pw) < 10:
        raise ValueError("Password must be at least 10 characters long")


def client_ip():
    return request.remote_addr or "-"


def _pw_version(password_hash):
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def load_user():
    uid = session.get("uid")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not row["active"] or session.get("pwv") != _pw_version(row["password_hash"]):
        session.clear()
        return None
    return row


def role_at_least(user, role):
    return ROLES.index(user["role"]) >= ROLES.index(role)


def require(role="viewer"):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            user = load_user()
            if user is None:
                return jsonify(error="Authentication required"), 401
            if not role_at_least(user, role):
                return jsonify(error="Insufficient privileges (requires %s)" % role), 403
            g.user = user
            return fn(*a, **kw)
        return wrapper
    return deco


def csrf_protect():
    """Applied to every state-changing /api request except login."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.path == "/api/login":
        return None
    token = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(token, expected):
        return jsonify(error="Invalid or missing CSRF token"), 403
    return None


def _locked_out(username, ip):
    cfg = current_app.config["WEBUI"]
    since = int(time.time()) - cfg.lockout_minutes * 60
    db = get_db()
    db.execute("DELETE FROM login_failures WHERE ts < ?", (since,))
    n = db.execute("SELECT COUNT(*) FROM login_failures WHERE (username=? OR ip=?) AND ts >= ?",
                   (username, ip, since)).fetchone()[0]
    return n >= cfg.max_login_failures


@bp.route("/api/login", methods=["POST"])
def login():
    if not request.is_json:
        return jsonify(error="JSON body required"), 400
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    ip = client_ip()
    db = get_db()
    if _locked_out(username, ip):
        audit(username, ip, None, "login", "locked out", success=False)
        return jsonify(error="Too many failed attempts. Try again later."), 429
    row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
    if not row or not ok or not row["active"]:
        db.execute("INSERT INTO login_failures (username, ip, ts) VALUES (?,?,?)",
                   (username, ip, int(time.time())))
        db.commit()
        audit(username, ip, None, "login", "invalid credentials", success=False)
        return jsonify(error="Invalid username or password"), 401
    session.clear()
    session.permanent = True
    session["uid"] = row["id"]
    session["pwv"] = _pw_version(row["password_hash"])
    session["csrf"] = secrets.token_urlsafe(32)
    db.execute("UPDATE users SET last_login=? WHERE id=?", (int(time.time()), row["id"]))
    db.execute("DELETE FROM login_failures WHERE username=?", (username,))
    db.commit()
    audit(username, ip, None, "login", "")
    return jsonify(me_payload(row))


@bp.route("/api/logout", methods=["POST"])
def logout():
    user = load_user()
    if user:
        audit(user["username"], client_ip(), None, "logout", "")
    session.clear()
    return jsonify(ok=True)


def me_payload(row):
    return {"username": row["username"], "role": row["role"], "csrf": session.get("csrf")}


@bp.route("/api/me")
@require("viewer")
def me():
    return jsonify(me_payload(g.user))


@bp.route("/api/me/password", methods=["POST"])
@require("viewer")
def change_own_password():
    data = request.get_json(silent=True) or {}
    if not check_password_hash(g.user["password_hash"], str(data.get("current", ""))):
        return jsonify(error="Current password is incorrect"), 400
    new = str(data.get("new", ""))
    try:
        validate_new_password(new)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    h = hash_password(new)
    db = get_db()
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (h, g.user["id"]))
    db.commit()
    session["pwv"] = _pw_version(h)
    audit(g.user["username"], client_ip(), None, "password.change", "own password")
    return jsonify(ok=True)


# ---- web UI account management (admin only) ----

@bp.route("/api/webui-users")
@require("admin")
def list_users():
    rows = get_db().execute("SELECT id, username, role, active, created_at, last_login "
                            "FROM users ORDER BY username").fetchall()
    return jsonify(rows=[dict(r) for r in rows], roles=ROLES)


@bp.route("/api/webui-users", methods=["POST"])
@require("admin")
def create_user():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    role = data.get("role", "viewer")
    if not username or len(username) > 64:
        return jsonify(error="Invalid username"), 400
    if role not in ROLES:
        return jsonify(error="Invalid role"), 400
    try:
        validate_new_password(str(data.get("password", "")))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password_hash, role, active, created_at) "
                   "VALUES (?,?,?,1,?)",
                   (username, hash_password(str(data["password"])), role, int(time.time())))
        db.commit()
    except Exception:
        return jsonify(error="User already exists"), 409
    audit(g.user["username"], client_ip(), None, "webui_user.create", "%s (%s)" % (username, role))
    return jsonify(ok=True)


@bp.route("/api/webui-users/<int:uid>", methods=["PUT"])
@require("admin")
def update_user(uid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify(error="Not found"), 404
    role = data.get("role", row["role"])
    active = 1 if data.get("active", row["active"]) else 0
    if role not in ROLES:
        return jsonify(error="Invalid role"), 400
    if uid == g.user["id"] and (role != "admin" or not active):
        return jsonify(error="You cannot demote or disable your own account"), 400
    db.execute("UPDATE users SET role=?, active=? WHERE id=?", (role, active, uid))
    changes = ["role=%s" % role, "active=%s" % active]
    if data.get("password"):
        try:
            validate_new_password(str(data["password"]))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (hash_password(str(data["password"])), uid))
        changes.append("password reset")
    db.commit()
    audit(g.user["username"], client_ip(), None, "webui_user.update",
          "%s: %s" % (row["username"], ", ".join(changes)))
    return jsonify(ok=True)


@bp.route("/api/webui-users/<int:uid>", methods=["DELETE"])
@require("admin")
def delete_user(uid):
    if uid == g.user["id"]:
        return jsonify(error="You cannot delete your own account"), 400
    db = get_db()
    row = db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify(error="Not found"), 404
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    audit(g.user["username"], client_ip(), None, "webui_user.delete", row["username"])
    return jsonify(ok=True)


@bp.route("/api/audit")
@require("admin")
def audit_log():
    limit = min(int(request.args.get("limit", 500)), 5000)
    rows = get_db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify(rows=[dict(r) for r in rows])
