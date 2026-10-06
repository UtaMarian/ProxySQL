"""Scenario tests against the Docker test stack (docker compose up -d --build first).

Covers load/save layers, ProxySQL Cluster propagation, 3 MariaDB backends, failover reactions.
Run: python docker/tests/scenarios.py
"""
import http.cookiejar, json, os, subprocess, time, urllib.request

B = "http://localhost:18080"
COMPOSE = ["docker", "compose", "-f", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docker-compose.yml")]
FAILS = []
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
CSRF = None


def api(m, p, b=None):
    h = {"Content-Type": "application/json"}
    if CSRF and m != "GET":
        h["X-CSRF-Token"] = CSRF
    r = urllib.request.Request(B + p, data=json.dumps(b).encode() if b is not None else None, method=m, headers=h)
    try:
        with op.open(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def sql(node, q):
    s, d = api("POST", "/api/i/%s/sql" % node, {"sql": q})
    if s != 200:
        raise RuntimeError("%s: %s" % (q, d))
    return [dict(zip(d["columns"], r)) for r in d["rows"]]


def check(name, cond, info=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> " + str(info)[:500]))
    if not cond:
        FAILS.append(name)


def wait(fn, timeout=20, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def pending(node, module):
    s, d = api("GET", "/api/i/%s/sync-status" % node)
    m = [x for x in d["modules"] if x["module"] == module][0]
    return m["runtime"], m["disk"]


def mdb(n, q):
    out = subprocess.run(COMPOSE + ["exec", "-T", "mariadb%d" % n, "mariadb", "-uroot", "-prootpass", "-N", "-e", q],
                         capture_output=True, text=True)
    return (out.stdout + out.stderr).strip()


def client(node_port_host, user, pw, q):
    out = subprocess.run(COMPOSE + ["exec", "-T", "traffic", "mariadb", "--skip-ssl", "-h", node_port_host, "-P", "6033",
                                    "-u" + user, "-p" + pw, "-N", "-e", q], capture_output=True, text=True)
    return (out.stdout + out.stderr).strip()


s, d = api("POST", "/api/login", {"username": "admin", "password": "AdminPass123"})
CSRF = d["csrf"]
U = "cluster_test"

print("\n== 0. Baseline")
for n in ("proxysql1", "proxysql2"):
    s, d = api("GET", "/api/i/%s/sync-status" % n)
    dirty = [(m["module"], m["runtime"], m["disk"]) for m in d["modules"] if m["runtime"] or m["disk"]]
    check(n + " all modules in sync at start (incl. MYSQL SERVERS with replication hostgroups)", not dirty, dirty)
rt = sql("proxysql1", "SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers ORDER BY hostgroup_id, hostname")
print("   runtime servers:", [(r["hostgroup_id"], r["hostname"], r["status"]) for r in rt])
check("3 backends ONLINE in runtime", {r["hostname"] for r in rt if r["status"] == "ONLINE"} == {"mariadb1", "mariadb2", "mariadb3"}, rt)
peers = sql("proxysql1", "SELECT hostname, name, version, diff_check FROM stats_proxysql_servers_checksums WHERE name='mysql_users'")
check("cluster peers visible in stats_proxysql_servers_checksums", len(peers) == 2, peers)

print("\n== 1. Create a user on node 1 in MEMORY only")
mdb(1, "CREATE USER IF NOT EXISTS '%s'@'%%' IDENTIFIED BY 'TestPass1'; GRANT SELECT ON appdb.* TO '%s'@'%%';" % (U, U))
time.sleep(1)
check("backend user replicated to mariadb3", U in mdb(3, "SELECT user FROM mysql.user WHERE user='%s'" % U))
api("DELETE", "/api/i/proxysql1/table/mysql_users", {"key": {"username": U, "backend": 1, "frontend": 1}, "apply": ["load_runtime", "save_disk"]})
check("cleanup: previous run's user gone from node2 runtime",
      wait(lambda: not sql("proxysql2", "SELECT 1 FROM runtime_mysql_users WHERE username='%s'" % U), 30))
s, d = api("POST", "/api/i/proxysql1/table/mysql_users",
           {"values": {"username": U, "password": "TestPass1", "default_hostgroup": "20", "default_schema": "appdb"}})
check("insert user (memory)", s == 200 and d["applied"] == [], d)
check("node1 pending: runtime 1 / disk 1", pending("proxysql1", "MYSQL USERS") == (1, 1), pending("proxysql1", "MYSQL USERS"))
time.sleep(3)
check("node2 does NOT get memory-only change", not sql("proxysql2", "SELECT username FROM mysql_users WHERE username='%s'" % U))
check("client login via node1 fails before LOAD TO RUNTIME", "denied" in client("proxysql1", U, "TestPass1", "SELECT 1"))

print("\n== 2. LOAD MYSQL USERS TO RUNTIME on node 1")
s, d = api("POST", "/api/i/proxysql1/command", {"action": "load_runtime", "module": "MYSQL USERS"})
check("command ok", d["ok"], d)
check("node1 pending now: runtime 0 / disk 1", pending("proxysql1", "MYSQL USERS") == (0, 1), pending("proxysql1", "MYSQL USERS"))
check("client login via node1 works after LOAD TO RUNTIME", client("proxysql1", U, "TestPass1", "SELECT 'ok1'") == "ok1")
t0 = time.time()
got = wait(lambda: sql("proxysql2", "SELECT username FROM runtime_mysql_users WHERE username='%s'" % U), 30)
check("node2 RUNTIME received the user via ProxySQL Cluster (%.1fs)" % (time.time() - t0), bool(got))
check("node2 MEMORY has the user", bool(sql("proxysql2", "SELECT username FROM mysql_users WHERE username='%s'" % U)))
check("node2 DISK has the user (cluster_mysql_users_save_to_disk)",
      bool(wait(lambda: sql("proxysql2", "SELECT username FROM disk.mysql_users WHERE username='%s'" % U), 10)))
check("node2 shows MYSQL USERS in sync", pending("proxysql2", "MYSQL USERS") == (0, 0), pending("proxysql2", "MYSQL USERS"))
check("client login via node2 works", wait(lambda: client("proxysql2", U, "TestPass1", "SELECT 'ok2'") == "ok2", 10))
print("   node2 password stored as:", sql("proxysql2", "SELECT password FROM mysql_users WHERE username='%s'" % U)[0]["password"][:12] + "...")

print("\n== 3. SAVE MYSQL USERS TO DISK on node 1")
s, d = api("POST", "/api/i/proxysql1/command", {"action": "save_disk", "module": "MYSQL USERS"})
check("node1 fully in sync", d["ok"] and pending("proxysql1", "MYSQL USERS") == (0, 0), pending("proxysql1", "MYSQL USERS"))
s, d = api("GET", "/api/i/proxysql1/table/mysql_users?source=disk")
check("user visible in Disk view of the table", any(r["username"] == U for r in d["rows"]), d)

print("\n== 4. LOAD FROM DISK reverts an unsaved memory edit")
api("PUT", "/api/i/proxysql1/table/mysql_users", {"key": {"username": U, "backend": 1, "frontend": 1}, "values": {"max_connections": "77"}})
check("memory edited (pending 1/1)", pending("proxysql1", "MYSQL USERS") == (1, 1))
api("POST", "/api/i/proxysql1/command", {"action": "load_disk", "module": "MYSQL USERS"})
mc = sql("proxysql1", "SELECT max_connections FROM mysql_users WHERE username='%s'" % U)[0]["max_connections"]
check("max_connections restored from disk (10000)", mc == "10000", mc)

print("\n== 5. SAVE FROM RUNTIME reverts an unsaved memory edit")
api("PUT", "/api/i/proxysql1/table/mysql_users", {"key": {"username": U, "backend": 1, "frontend": 1}, "values": {"comment": "temp edit"}})
api("POST", "/api/i/proxysql1/command", {"action": "save_memory", "module": "MYSQL USERS"})
cm = sql("proxysql1", "SELECT comment FROM mysql_users WHERE username='%s'" % U)[0]["comment"]
check("comment restored from runtime", cm == "", cm)
print("   note: SAVE FROM RUNTIME copies runtime passwords (hashed) into memory:",
      sql("proxysql1", "SELECT password FROM mysql_users WHERE username='%s'" % U)[0]["password"][:12] + "...")
check("MYSQL USERS memory==runtime after SAVE FROM RUNTIME", pending("proxysql1", "MYSQL USERS")[0] == 0)
raw = sql("proxysql1", "SELECT frontend, backend FROM mysql_users WHERE username='%s'" % U)
s, d = api("GET", "/api/i/proxysql1/table/mysql_users")
view = [r for r in d["rows"] if r["username"] == U]
print("   after SAVE FROM RUNTIME memory holds %d rows: %s" % (len(raw), [(r["frontend"], r["backend"]) for r in raw]))
check("split rows shown as ONE user in the UI", len(raw) == 2 and len(view) == 1, (raw, view))
s, d = api("PUT", "/api/i/proxysql1/table/mysql_users", {"key": {"username": U, "backend": 1, "frontend": 1}, "values": {"max_connections": "500", "frontend": "1", "backend": "1"}})
check("edit of split user updates both rows", d.get("affected") == 2 and
      [r["max_connections"] for r in sql("proxysql1", "SELECT max_connections FROM mysql_users WHERE username='%s'" % U)] == ["500", "500"], d)
api("PUT", "/api/i/proxysql1/table/mysql_users", {"key": {"username": U, "backend": 1, "frontend": 1}, "values": {"max_connections": "10000"}, "apply": ["load_runtime"]})
api("POST", "/api/i/proxysql1/command", {"action": "save_disk", "module": "MYSQL USERS"})

print("\n== 6. Edit with 'Load to runtime + Save to disk' checkboxes (query rule) propagates to node 2")
api("DELETE", "/api/i/proxysql1/table/mysql_query_rules", {"key": {"rule_id": 150}, "apply": ["load_runtime", "save_disk"]})
s, d = api("POST", "/api/i/proxysql1/table/mysql_query_rules",
           {"values": {"rule_id": "150", "active": "1", "match_digest": "^SELECT COUNT", "destination_hostgroup": "20", "apply": "1", "comment": "count to readers"},
            "apply": ["load_runtime", "save_disk"]})
check("insert + applied 2 commands", s == 200 and len(d["applied"]) == 2, d)
check("node1 QUERY RULES in sync", pending("proxysql1", "MYSQL QUERY RULES") == (0, 0), pending("proxysql1", "MYSQL QUERY RULES"))
check("node2 runtime got rule 150", bool(wait(lambda: sql("proxysql2", "SELECT rule_id FROM runtime_mysql_query_rules WHERE rule_id=150"), 30)))
check("node2 disk got rule 150", bool(wait(lambda: sql("proxysql2", "SELECT rule_id FROM disk.mysql_query_rules WHERE rule_id=150"), 10)))

print("\n== 7. Variable change propagates (mysql-max_connections)")
s, d = api("PUT", "/api/i/proxysql1/variables", {"name": "mysql-max_connections", "value": "3000", "apply": ["load_runtime", "save_disk"]})
check("node1 runtime value", d.get("runtime") == "3000", d)
check("node2 runtime got 3000", bool(wait(lambda: sql("proxysql2", "SELECT 1 FROM runtime_global_variables WHERE variable_name='mysql-max_connections' AND variable_value='3000'"), 30)))

print("\n== 8. Change made on node 2 flows back to node 1")
raw = sql("proxysql2", "SELECT frontend, backend FROM mysql_users WHERE username='%s'" % U)
s, d = api("GET", "/api/i/proxysql2/table/mysql_users")
view = [r for r in d["rows"] if r["username"] == U]
print("   node2 stores the synced user as %d row(s): %s" % (len(raw), [(r["frontend"], r["backend"]) for r in raw]))
check("UI shows the synced user as ONE row (frontend+backend)", len(view) == 1 and view[0]["frontend"] == "1" and view[0]["backend"] == "1", view)
s, d = api("PUT", "/api/i/proxysql2/table/mysql_users", {"key": {"username": U, "backend": 1, "frontend": 1}, "values": {"comment": "edited on node2", "frontend": "1", "backend": "1"}, "apply": ["load_runtime", "save_disk"]})
check("edit hits every row of the user", s == 200 and d["affected"] == len(raw), d)
check("no half-edited rows on node2", all(r["comment"] == "edited on node2" for r in sql("proxysql2", "SELECT comment FROM mysql_users WHERE username='%s'" % U)))
check("node1 got comment from node2", bool(wait(lambda: sql("proxysql1", "SELECT 1 FROM runtime_mysql_users WHERE username='%s' AND comment='edited on node2'" % U), 30)))

print("\n== 8b. Change frontend/backend of a (split) user")
s, d = api("PUT", "/api/i/proxysql1/table/mysql_users", {"key": {"username": U, "backend": 1, "frontend": 1}, "values": {"frontend": "1", "backend": "0"}})
rows = sql("proxysql1", "SELECT frontend, backend FROM mysql_users WHERE username='%s'" % U)
check("user rewritten as a single frontend-only row", s == 200 and [(r["frontend"], r["backend"]) for r in rows] == [("1", "0")], (d, rows))
s, d = api("PUT", "/api/i/proxysql1/table/mysql_users", {"key": {"username": U, "backend": 0, "frontend": 1}, "values": {"backend": "1"}, "apply": ["load_runtime", "save_disk"]})
rows = sql("proxysql1", "SELECT frontend, backend FROM mysql_users WHERE username='%s'" % U)
check("back to frontend+backend", [(r["frontend"], r["backend"]) for r in rows] == [("1", "1")], rows)
check("client login via node2 still works", wait(lambda: client("proxysql2", U, "TestPass1", "SELECT 'ok'") == "ok", 15))

print("\n== 9. Three backends: read distribution and replication")
api("POST", "/api/i/proxysql1/stats/pool/reset")
time.sleep(8)
pool = sql("proxysql1", "SELECT hostgroup, srv_host, status, Queries FROM stats_mysql_connection_pool ORDER BY hostgroup, srv_host")
print("   pool:", [(p["hostgroup"], p["srv_host"], p["status"], p["Queries"]) for p in pool])
readers = {p["srv_host"] for p in pool if p["hostgroup"] == "20" and int(p["Queries"]) > 0}
check("reads spread over replicas in HG 20", {"mariadb2", "mariadb3"} <= readers, pool)
check("writes go to mariadb1 in HG 10", any(p["hostgroup"] == "10" and p["srv_host"] == "mariadb1" and int(p["Queries"]) > 0 for p in pool), pool)
c1, c3 = mdb(1, "SELECT COUNT(*) FROM appdb.orders"), mdb(3, "SELECT COUNT(*) FROM appdb.orders")
print("   orders rows: primary=%s replica2=%s" % (c1, c3))
check("replicas receive writes", int(c3) > 0 and abs(int(c1) - int(c3)) < 20)
s, d = api("GET", "/api/i/proxysql1/monitor/read_only?limit=6")
print("   read_only checks:", sorted({(r["hostname"], r["read_only"]) for r in d["rows"]}))

print("\n== 10. Replica outage: stop mariadb3")
subprocess.run(COMPOSE + ["stop", "mariadb3"], capture_output=True)
st = wait(lambda: [p for p in sql("proxysql1", "SELECT srv_host, status FROM stats_mysql_connection_pool WHERE srv_host='mariadb3'") if p["status"] != "ONLINE"], 40)
check("mariadb3 marked SHUNNED/offline", bool(st), st)
print("   ", st)
check("clients still served (reads go to remaining nodes)", client("proxysql1", "app", "apppass", "SELECT COUNT(*) FROM appdb.orders").isdigit())
s, d = api("GET", "/api/i/proxysql1/monitor/ping?limit=30")
errs = [r for r in d["rows"] if r["hostname"] == "mariadb3" and r["ping_error"]]
check("monitor ping log shows mariadb3 errors", bool(errs), d["rows"][:3])
check("sync-status does not flag runtime status change", pending("proxysql1", "MYSQL SERVERS") == (0, 0), pending("proxysql1", "MYSQL SERVERS"))
subprocess.run(COMPOSE + ["start", "mariadb3"], capture_output=True)
back = wait(lambda: all(p["status"] == "ONLINE" for p in sql("proxysql1", "SELECT status FROM stats_mysql_connection_pool WHERE srv_host='mariadb3'")), 60)
check("mariadb3 back ONLINE after restart", back)

print("\n== 11. Primary set read_only=1: monitor removes it from writer HG; diff stays clean")
mdb(1, "SET GLOBAL read_only=1")
moved = wait(lambda: not sql("proxysql1", "SELECT 1 FROM runtime_mysql_servers WHERE hostgroup_id=10"), 20)
check("HG 10 (writer) emptied in runtime", moved, sql("proxysql1", "SELECT hostgroup_id, hostname FROM runtime_mysql_servers"))
check("MYSQL SERVERS not reported as pending (managed hostgroups)", pending("proxysql1", "MYSQL SERVERS") == (0, 0), pending("proxysql1", "MYSQL SERVERS"))
mdb(1, "SET GLOBAL read_only=0")
check("mariadb1 back in writer HG 10", bool(wait(lambda: sql("proxysql1", "SELECT 1 FROM runtime_mysql_servers WHERE hostgroup_id=10 AND hostname='mariadb1'"), 20)))

print("\n== 12. Add a server in memory: real change IS reported")
api("POST", "/api/i/proxysql1/table/mysql_servers", {"values": {"hostgroup_id": "20", "hostname": "mariadb9", "port": "3306", "comment": "temp"}})
check("pending runtime/disk = 1/1", pending("proxysql1", "MYSQL SERVERS") == (1, 1), pending("proxysql1", "MYSQL SERVERS"))
api("DELETE", "/api/i/proxysql1/table/mysql_servers", {"key": {"hostgroup_id": 20, "hostname": "mariadb9", "port": 3306}})
check("clean again after delete", pending("proxysql1", "MYSQL SERVERS") == (0, 0))

print("\n%d failures" % len(FAILS), FAILS)
