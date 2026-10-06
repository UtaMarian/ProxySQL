"""REST API for ProxySQL configuration, statistics, monitor data and service control."""
import hashlib

from flask import Blueprint, abort, current_app, g, jsonify, request

from . import service
from .auth import client_ip, require
from .db import audit
from .proxysql import AdminClient, ProxySQLError, ident, lit
from .schemas import (ACTIONS, MODULE_KEYS, MODULES, RUNTIME_IGNORE, TABLES,
                      VARIABLE_MODULE, VARIABLE_PREFIXES)

bp = Blueprint("api", __name__, url_prefix="/api")


def cfg():
    return current_app.config["WEBUI"]


def instance(key):
    inst = cfg().instances.get(key)
    if inst is None:
        abort(404, description="Unknown ProxySQL instance")
    return inst


def body():
    return request.get_json(silent=True) or {}


def log(inst_key, action, detail, success=True):
    audit(g.user["username"], client_ip(), inst_key, action, detail, success)


def module_sql(module, action):
    if module not in MODULE_KEYS and module not in VARIABLE_MODULE.values():
        raise ValueError("Unknown module %s" % module)
    if action not in ACTIONS:
        raise ValueError("Unknown action %s" % action)
    return ACTIONS[action][0].format(m=module)


def run_apply(cli, inst_key, module, actions):
    """Optionally push a change to runtime and/or disk right after editing."""
    done = []
    for a in actions or []:
        if a not in ("load_runtime", "save_disk"):
            continue
        sql = module_sql(module, a)
        cli.execute(sql)
        log(inst_key, "config." + a, sql)
        done.append(sql)
    return done


# ---------------------------------------------------------------- instances

@bp.route("/instances")
@require("viewer")
def instances():
    c = cfg()
    return jsonify(instances=[i.public() for i in c.instances.values()],
                   service_enabled=c.service_enabled, service_unit=c.service_unit)


@bp.route("/i/<key>/ping")
@require("viewer")
def ping(key):
    inst = instance(key)
    try:
        with AdminClient(inst) as cli:
            rows = cli.rows("SELECT variable_value AS v FROM global_variables "
                            "WHERE variable_name='admin-version'")
        return jsonify(ok=True, version=rows[0]["v"] if rows else None)
    except ProxySQLError as e:
        return jsonify(ok=False, error=str(e))


@bp.route("/i/<key>/overview")
@require("viewer")
def overview(key):
    with AdminClient(instance(key)) as cli:
        glob = {r["Variable_Name"]: r["Variable_Value"]
                for r in cli.rows("SELECT Variable_Name, Variable_Value FROM stats_mysql_global")}
        ver = cli.rows("SELECT variable_value AS v FROM global_variables WHERE variable_name='admin-version'")
        pool = cli.rows("SELECT * FROM stats_mysql_connection_pool ORDER BY hostgroup, srv_host, srv_port")
        servers = cli.rows("SELECT hostgroup_id, hostname, port, status, weight, max_connections, comment "
                           "FROM runtime_mysql_servers ORDER BY hostgroup_id, hostname, port")
        counts = {}
        for t in ("runtime_mysql_users", "runtime_mysql_query_rules", "runtime_proxysql_servers"):
            try:
                counts[t] = int(cli.rows("SELECT COUNT(*) AS n FROM %s" % t)[0]["n"])
            except ProxySQLError:
                counts[t] = None
    return jsonify(version=ver[0]["v"] if ver else None, glob=glob, pool=pool,
                   servers=servers, counts=counts)


# ---------------------------------------------------------------- generic config tables

def _table(name):
    t = TABLES.get(name)
    if t is None:
        abort(404, description="Unknown table")
    return t


def _schema_for(t, actual_cols):
    known = {col["name"]: col for col in t["columns"]}
    cols = [col for col in t["columns"] if col["name"] in actual_cols]
    for name in actual_cols:
        if name not in known:
            cols.append({"name": name, "label": name, "type": "text", "default": None,
                         "nullable": True, "required": False, "choices": None,
                         "help": "Column from a newer ProxySQL version", "advanced": True})
    return cols


def _coerce(col, value):
    t = col["type"]
    if value is None or (isinstance(value, str) and value.strip() == "" and t != "password"):
        if col.get("nullable"):
            return None
        if col.get("required"):
            raise ValueError("%s is required" % col["name"])
        return col.get("default") if col.get("default") is not None else ("" if t in ("text", "textarea") else 0)
    if t == "int":
        try:
            return int(str(value).strip())
        except ValueError:
            raise ValueError("%s must be an integer" % col["name"])
    if t == "bool":
        return 1 if str(value).lower() in ("1", "true", "yes", "on") else 0
    if t == "enum":
        v = str(value)
        if col.get("choices") and v not in col["choices"]:
            raise ValueError("%s must be one of %s" % (col["name"], ", ".join(col["choices"])))
        return v
    return str(value)


def native_password_hash(pw):
    return "*" + hashlib.sha1(hashlib.sha1(pw.encode("utf-8")).digest()).hexdigest().upper()


def _prepare_values(t, schema, values, is_update):
    out = {}
    by_name = {col["name"]: col for col in schema}
    for name, raw in values.items():
        col = by_name.get(name)
        if col is None:
            continue
        if col["type"] == "password":
            if raw in (None, ""):
                if is_update:
                    continue  # keep current password
                raw = ""
            out[name] = str(raw)
            continue
        if col["help"] == "Column from a newer ProxySQL version" and raw in (None, ""):
            continue
        out[name] = _coerce(col, raw)
    for col in schema:
        if col.get("required") and not is_update and out.get(col["name"]) in (None, ""):
            raise ValueError("%s is required" % col["name"])
    return out


def _where(t, schema, key):
    by_name = {col["name"]: col for col in schema}
    parts = []
    for k in t["pk"]:
        if k not in by_name:
            continue
        if k not in key:
            raise ValueError("Missing key column %s" % k)
        v = key[k]
        if v is None:
            parts.append("%s IS NULL" % ident(k))
        else:
            if by_name[k]["type"] in ("int", "bool"):
                v = int(v)
            parts.append("%s=%s" % (ident(k), lit(v)))
    return " AND ".join(parts)


def _masked(t, values):
    secret = t.get("secret", [])
    return {k: ("***" if k in secret else v) for k, v in values.items()}


def _set_sql(values):
    return ", ".join("%s=%s" % (ident(k), lit(v)) for k, v in values.items())


def _merge_user_rows(rows):
    """Show a user split into a frontend-only and a backend-only row as one user.

    ProxySQL Cluster sync and SAVE MYSQL USERS FROM RUNTIME store every
    frontend+backend user as two rows; editing just one of them would change half a user.
    """
    out, split = [], {}
    for r in rows:
        if str(r.get("frontend")) == "1" and str(r.get("backend")) == "1":
            out.append(r)
        else:
            split.setdefault(r["username"], []).append(r)
    for rs in split.values():
        if len(rs) == 2:
            a, b = rs
            same = all(a[k] == b[k] for k in a if k not in ("frontend", "backend"))
            roles = {(str(a["frontend"]), str(a["backend"])), (str(b["frontend"]), str(b["backend"]))}
            if same and roles == {("1", "0"), ("0", "1")}:
                out.append(dict(a, frontend="1", backend="1"))
                continue
        out.extend(rs)
    return sorted(out, key=lambda r: (r["username"], str(r.get("frontend")), str(r.get("backend"))))


def _users_update(cli, key_name, t, values, username):
    """Update every row of a MySQL user; frontend/backend changes rewrite it as a single row."""
    stmts = []
    role = {k: int(values.pop(k)) for k in ("frontend", "backend") if k in values}
    where = "username=%s" % lit(username)
    if values:
        stmts.append(("UPDATE mysql_users SET %s WHERE %s" % (_set_sql(values), where),
                      "UPDATE mysql_users SET %s WHERE %s" % (_set_sql(_masked(t, values)), where)))
        cli.execute(stmts[-1][0])
    rows = cli.rows("SELECT * FROM mysql_users WHERE %s" % where)
    if not rows:
        return 0, stmts
    have = (max(int(r["frontend"]) for r in rows), max(int(r["backend"]) for r in rows))
    want = (role.get("frontend", have[0]), role.get("backend", have[1]))
    if want == (0, 0):
        raise ValueError("A user must be a frontend user, a backend user, or both")
    if want != have:
        base = dict(rows[0], frontend=want[0], backend=want[1])
        cols = list(base.keys())
        ins = "INSERT INTO mysql_users (%s) VALUES (%%s)" % ", ".join(ident(c) for c in cols)
        stmts.append(("DELETE FROM mysql_users WHERE %s" % where,) * 2)
        stmts.append((ins % ", ".join(lit(base[c]) for c in cols),
                      ins % ", ".join(lit(_masked(t, base)[c]) for c in cols)))
        for sql, _ in stmts[-2:]:
            cli.execute(sql)
    return len(rows), stmts


@bp.route("/i/<key>/table/<name>")
@require("viewer")
def table_get(key, name):
    t = _table(name)
    source = request.args.get("source", "memory")
    prefix = {"memory": "", "runtime": "runtime_", "disk": "disk."}.get(source)
    if prefix is None:
        abort(400, description="Invalid source")
    with AdminClient(instance(key)) as cli:
        actual = cli.columns(name)
        rows = cli.rows("SELECT * FROM %s%s ORDER BY %s" % (prefix, ident(name), t["order"]))
    if name == "mysql_users":
        rows = _merge_user_rows(rows)
    hide = set(t.get("secret", [])) if g.user["role"] == "viewer" else set()
    if hide:
        rows = [{k: ("***" if k in hide and v else v) for k, v in r.items()} for r in rows]
    return jsonify(table=name, label=t["label"], module=t["module"], pk=t["pk"],
                   columns=_schema_for(t, actual), rows=rows, source=source)


@bp.route("/i/<key>/table/<name>", methods=["POST"])
@require("operator")
def table_insert(key, name):
    t = _table(name)
    data = body()
    with AdminClient(instance(key)) as cli:
        schema = _schema_for(t, cli.columns(name))
        values = _prepare_values(t, schema, data.get("values", {}), False)
        if name == "mysql_users" and data.get("hash_password") and values.get("password"):
            values["password"] = native_password_hash(values["password"])
        cols = list(values.keys())
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (
            ident(name), ", ".join(ident(c) for c in cols), ", ".join(lit(values[c]) for c in cols))
        masked = _masked(t, values)
        audit_sql = "INSERT INTO %s (%s) VALUES (%s)" % (
            name, ", ".join(cols), ", ".join(lit(masked[c]) for c in cols))
        try:
            cli.execute(sql)
        except ProxySQLError as e:
            log(key, "table.insert", "%s -- %s" % (audit_sql, e), False)
            raise
        log(key, "table.insert", audit_sql)
        applied = run_apply(cli, key, t["module"], data.get("apply"))
    return jsonify(ok=True, applied=applied)


@bp.route("/i/<key>/table/<name>", methods=["PUT"])
@require("operator")
def table_update(key, name):
    t = _table(name)
    data = body()
    with AdminClient(instance(key)) as cli:
        schema = _schema_for(t, cli.columns(name))
        values = _prepare_values(t, schema, data.get("values", {}), True)
        if name == "mysql_users" and data.get("hash_password") and values.get("password"):
            values["password"] = native_password_hash(values["password"])
        if not values:
            raise ValueError("Nothing to update")
        if name == "mysql_users":
            username = (data.get("key") or {}).get("username")
            if not username:
                raise ValueError("Missing key column username")
            try:
                n, stmts = _users_update(cli, key, t, values, username)
            except ProxySQLError as e:
                log(key, "table.update", "mysql_users %s -- %s" % (username, e), False)
                raise
            for _, audit_sql in stmts:
                log(key, "table.update", audit_sql)
            applied = run_apply(cli, key, t["module"], data.get("apply"))
            return jsonify(ok=True, affected=n, applied=applied)
        where = _where(t, schema, data.get("key", {}))
        sql = "UPDATE %s SET %s WHERE %s" % (ident(name), _set_sql(values), where)
        audit_sql = "UPDATE %s SET %s WHERE %s" % (name, _set_sql(_masked(t, values)), where)
        try:
            n = cli.execute(sql)
        except ProxySQLError as e:
            log(key, "table.update", "%s -- %s" % (audit_sql, e), False)
            raise
        log(key, "table.update", audit_sql)
        applied = run_apply(cli, key, t["module"], data.get("apply"))
    return jsonify(ok=True, affected=n, applied=applied)


@bp.route("/i/<key>/table/<name>", methods=["DELETE"])
@require("operator")
def table_delete(key, name):
    t = _table(name)
    data = body()
    with AdminClient(instance(key)) as cli:
        schema = _schema_for(t, cli.columns(name))
        if name == "mysql_users":
            # remove the whole user (its frontend and backend rows)
            where = "username=%s" % lit((data.get("key") or {}).get("username") or "")
        else:
            where = _where(t, schema, data.get("key", {}))
        sql = "DELETE FROM %s WHERE %s" % (ident(name), where)
        n = cli.execute(sql)
        log(key, "table.delete", sql)
        applied = run_apply(cli, key, t["module"], data.get("apply"))
    return jsonify(ok=True, affected=n, applied=applied)


# ---------------------------------------------------------------- global variables

@bp.route("/i/<key>/variables")
@require("viewer")
def variables_get(key):
    with AdminClient(instance(key)) as cli:
        mem = cli.rows("SELECT variable_name, variable_value FROM global_variables ORDER BY variable_name")
        run = {r["variable_name"]: r["variable_value"] for r in
               cli.rows("SELECT variable_name, variable_value FROM runtime_global_variables")}
        try:
            disk = {r["variable_name"]: r["variable_value"] for r in
                    cli.rows("SELECT variable_name, variable_value FROM disk.global_variables")}
        except ProxySQLError:
            disk = {}
    hide = g.user["role"] == "viewer"
    rows = []
    for r in mem:
        n = r["variable_name"]
        secret = hide and ("password" in n or "credentials" in n)
        rows.append({"name": n,
                     "value": "***" if secret else r["variable_value"],
                     "runtime": "***" if secret else run.get(n),
                     "disk": "***" if secret else disk.get(n),
                     "module": next((m for p, m in VARIABLE_MODULE.items() if n.startswith(p)), None)})
    return jsonify(rows=rows, prefixes=VARIABLE_PREFIXES)


@bp.route("/i/<key>/variables", methods=["PUT"])
@require("operator")
def variables_set(key):
    data = body()
    name = str(data.get("name", ""))
    value = data.get("value")
    if value is None:
        raise ValueError("value is required")
    with AdminClient(instance(key)) as cli:
        if not cli.rows("SELECT 1 AS x FROM global_variables WHERE variable_name=%s" % lit(name)):
            raise ValueError("Unknown variable %s" % name)
        sql = "UPDATE global_variables SET variable_value=%s WHERE variable_name=%s" % (lit(str(value)), lit(name))
        cli.execute(sql)
        secret = "password" in name or "credentials" in name
        log(key, "variable.set", "%s = %s" % (name, "***" if secret else value))
        module = next((m for p, m in VARIABLE_MODULE.items() if name.startswith(p)), None)
        applied = run_apply(cli, key, module, data.get("apply")) if module else []
        # read back: ProxySQL may silently reject invalid values on LOAD TO RUNTIME
        rt = cli.rows("SELECT variable_value AS v FROM runtime_global_variables WHERE variable_name=%s" % lit(name))
    return jsonify(ok=True, applied=applied, runtime=rt[0]["v"] if rt else None)


# ---------------------------------------------------------------- load / save

@bp.route("/i/<key>/command", methods=["POST"])
@require("operator")
def command(key):
    data = body()
    action = data.get("action")
    modules = data.get("modules") or [data.get("module")]
    results = []
    with AdminClient(instance(key)) as cli:
        for m in modules:
            sql = module_sql(m, action)
            try:
                cli.execute(sql)
                log(key, "config." + action, sql)
                results.append({"module": m, "sql": sql, "ok": True})
            except ProxySQLError as e:
                log(key, "config." + action, "%s -- %s" % (sql, e), False)
                results.append({"module": m, "sql": sql, "ok": False, "error": str(e)})
    return jsonify(results=results, ok=all(r["ok"] for r in results))


def _rowset(rows, cols, ignore):
    use = [c for c in cols if c not in ignore]
    return use, set(tuple(None if r.get(c) is None else str(r.get(c)) for c in use) for r in rows)


MANAGED_HG_TABLES = {
    "mysql_replication_hostgroups": ["writer_hostgroup", "reader_hostgroup"],
    "mysql_galera_hostgroups": ["writer_hostgroup", "backup_writer_hostgroup", "reader_hostgroup", "offline_hostgroup"],
    "mysql_group_replication_hostgroups": ["writer_hostgroup", "backup_writer_hostgroup", "reader_hostgroup",
                                           "offline_hostgroup"],
}


def _managed_hostgroups(cli):
    """Map each hostgroup owned by a replication/Galera/GR definition to its cluster.

    The monitor moves servers between these hostgroups at runtime (writer also reader,
    read_only flips, Galera node states), so placement there is not a config difference.
    """
    out = {}
    for table, cols in MANAGED_HG_TABLES.items():
        for prefix in ("", "runtime_"):
            try:
                rows = cli.rows("SELECT * FROM %s%s" % (prefix, table))
            except ProxySQLError:
                continue
            for r in rows:
                for c in cols:
                    if r.get(c) is not None:
                        out[str(r[c])] = "managed:%s" % r["writer_hostgroup"]
    return out


def _normalize_managed(rows, managed):
    if not managed:
        return rows
    return [dict(r, hostgroup_id=managed.get(str(r.get("hostgroup_id")), r.get("hostgroup_id"))) for r in rows]


def _compare(cli, table, prefix, other):
    where = ""
    if prefix:
        where = " WHERE variable_name LIKE %s" % lit(prefix + "%")
    mem_cols, mem = cli.query("SELECT * FROM %s%s" % (table, where))
    other_table = ("runtime_" + table) if other == "runtime" else ("disk." + table)
    oth_cols, oth = cli.query("SELECT * FROM %s%s" % (other_table, where))
    ignore = RUNTIME_IGNORE.get(table, []) if other == "runtime" else []
    if table == "mysql_servers" and other == "runtime":
        managed = _managed_hostgroups(cli)
        mem, oth = _normalize_managed(mem, managed), _normalize_managed(oth, managed)
    cols = [c for c in mem_cols if c in oth_cols]
    use, a = _rowset(mem, cols, ignore)
    _, b = _rowset(oth, cols, ignore)
    only_mem = sorted(a - b, key=lambda x: [str(v) for v in x])
    only_oth = sorted(b - a, key=lambda x: [str(v) for v in x])
    return use, only_mem, only_oth


def _diff_count(table, cols, only_a, only_b):
    """Number of changed entries: a row edited in place counts once, not as removed + added."""
    pk = ["variable_name"] if table == "global_variables" else TABLES.get(table, {}).get("pk", [])
    idx = [cols.index(c) for c in pk if c in cols]
    if not idx:
        return len(only_a) + len(only_b)
    return len({tuple(r[i] for i in idx) for r in list(only_a) + list(only_b)})


def _module(m):
    for mod in MODULES:
        if mod["key"] == m:
            return mod
    abort(404, description="Unknown module")


@bp.route("/i/<key>/sync-status")
@require("viewer")
def sync_status(key):
    out = []
    with AdminClient(instance(key)) as cli:
        for mod in MODULES:
            entry = {"module": mod["key"], "runtime": 0, "disk": 0, "error": None}
            for t in mod["tables"]:
                try:
                    for other in ("runtime", "disk"):
                        cols, a, b = _compare(cli, t, mod.get("var_prefix"), other)
                        entry[other] += _diff_count(t, cols, a, b)
                except ProxySQLError as e:
                    if t == mod["tables"][0]:
                        entry["error"] = str(e)
            out.append(entry)
    return jsonify(modules=out, actions={k: v[1] for k, v in ACTIONS.items()})


@bp.route("/i/<key>/diff")
@require("viewer")
def diff(key):
    mod = _module(request.args.get("module"))
    other = request.args.get("against", "runtime")
    if other not in ("runtime", "disk"):
        abort(400, description="Invalid comparison target")
    tables = []
    with AdminClient(instance(key)) as cli:
        for t in mod["tables"]:
            try:
                cols, a, b = _compare(cli, t, mod.get("var_prefix"), other)
                hide = set(TABLES.get(t, {}).get("secret", [])) if g.user["role"] == "viewer" else set()
                idx = [i for i, c in enumerate(cols) if c in hide]

                def mask(rows):
                    return [[("***" if i in idx and v else v) for i, v in enumerate(r)] for r in rows]
                tables.append({"table": t, "columns": cols, "only_memory": mask(a), "only_other": mask(b)})
            except ProxySQLError as e:
                tables.append({"table": t, "error": str(e)})
    return jsonify(module=mod["key"], against=other, tables=tables)


# ---------------------------------------------------------------- statistics & monitor

STATS = {
    "pool": "SELECT * FROM stats_mysql_connection_pool ORDER BY hostgroup, srv_host, srv_port",
    "commands": "SELECT * FROM stats_mysql_commands_counters WHERE Total_cnt > 0 ORDER BY Total_cnt DESC",
    "digest": "SELECT * FROM stats_mysql_query_digest ORDER BY {order} DESC LIMIT {limit}",
    "processlist": "SELECT * FROM stats_mysql_processlist ORDER BY time_ms DESC",
    "users": "SELECT * FROM stats_mysql_users ORDER BY frontend_connections DESC, username",
    "errors": "SELECT * FROM stats_mysql_errors ORDER BY last_seen DESC LIMIT 500",
    "memory": "SELECT * FROM stats_memory_metrics",
    "global": "SELECT * FROM stats_mysql_global",
    "rules": "SELECT s.rule_id, s.hits, r.active, r.match_digest, r.match_pattern, "
             "r.destination_hostgroup, r.comment FROM stats_mysql_query_rules s "
             "LEFT JOIN runtime_mysql_query_rules r ON r.rule_id = s.rule_id ORDER BY s.rule_id",
    "free_connections": "SELECT * FROM stats_mysql_free_connections",
    "cluster_checksums": "SELECT * FROM stats_proxysql_servers_checksums ORDER BY hostname, port, name",
    "cluster_metrics": "SELECT * FROM stats_proxysql_servers_metrics ORDER BY hostname, port",
    "checksums": "SELECT * FROM runtime_checksums_values ORDER BY name",
    "gtid": "SELECT * FROM stats_mysql_gtid_executed",
}
DIGEST_ORDER = ["sum_time", "count_star", "max_time", "sum_rows_sent", "last_seen"]

MONITOR = {
    "ping": ("monitor.mysql_server_ping_log", "time_start_us"),
    "connect": ("monitor.mysql_server_connect_log", "time_start_us"),
    "read_only": ("monitor.mysql_server_read_only_log", "time_start_us"),
    "replication_lag": ("monitor.mysql_server_replication_lag_log", "time_start_us"),
    "galera": ("monitor.mysql_server_galera_log", "time_start_us"),
    "group_replication": ("monitor.mysql_server_group_replication_log", "time_start_us"),
}


@bp.route("/i/<key>/stats/<kind>")
@require("viewer")
def stats(key, kind):
    sql = STATS.get(kind)
    if sql is None:
        abort(404, description="Unknown statistics")
    if kind == "digest":
        order = request.args.get("order", "sum_time")
        if order not in DIGEST_ORDER:
            order = "sum_time"
        limit = max(1, min(int(request.args.get("limit", 200)), 5000))
        sql = sql.format(order=order, limit=limit)
    with AdminClient(instance(key)) as cli:
        cols, rows = cli.query(sql)
    return jsonify(columns=cols, rows=rows)


@bp.route("/i/<key>/stats/<kind>/reset", methods=["POST"])
@require("operator")
def stats_reset(key, kind):
    sql = {"digest": "SELECT COUNT(*) FROM stats_mysql_query_digest_reset",
           "errors": "SELECT COUNT(*) FROM stats_mysql_errors_reset",
           "pool": "SELECT COUNT(*) FROM stats_mysql_connection_pool_reset"}.get(kind)
    if sql is None:
        abort(404, description="Cannot reset this statistic")
    with AdminClient(instance(key)) as cli:
        cli.query(sql)
    log(key, "stats.reset", kind)
    return jsonify(ok=True)


@bp.route("/i/<key>/monitor/<kind>")
@require("viewer")
def monitor(key, kind):
    m = MONITOR.get(kind)
    if m is None:
        abort(404, description="Unknown monitor log")
    limit = max(1, min(int(request.args.get("limit", 200)), 5000))
    with AdminClient(instance(key)) as cli:
        cols, rows = cli.query("SELECT * FROM %s ORDER BY %s DESC LIMIT %d" % (m[0], m[1], limit))
    return jsonify(columns=cols, rows=rows)


# ---------------------------------------------------------------- SQL console

@bp.route("/i/<key>/sql", methods=["POST"])
@require("admin")
def sql_console(key):
    sql = str(body().get("sql", "")).strip()
    if not sql:
        raise ValueError("Empty statement")
    with AdminClient(instance(key)) as cli:
        try:
            res = cli.raw(sql)
        except ProxySQLError as e:
            log(key, "sql", "%s -- %s" % (sql, e), False)
            raise
    log(key, "sql", sql)
    if len(res["rows"]) > 5000:
        res["rows"] = res["rows"][:5000]
        res["truncated"] = True
    return jsonify(res)


# ---------------------------------------------------------------- local systemd service

def _require_service():
    if not cfg().service_enabled:
        abort(404, description="Service management is disabled in the configuration")


@bp.route("/service/status")
@require("viewer")
def service_status():
    _require_service()
    return jsonify(service.status(cfg()))


@bp.route("/service/<action>", methods=["POST"])
@require("admin")
def service_control(action):
    _require_service()
    res = service.control(cfg(), action)
    audit(g.user["username"], client_ip(), None, "service." + action,
          res["output"][:500], res["ok"])
    return jsonify(res)


@bp.route("/service/logs")
@require("operator")
def service_logs():
    _require_service()
    source = request.args.get("source", "file")
    return jsonify(service.logs(cfg(), "journal" if source == "journal" else "file"))
