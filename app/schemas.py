"""Editable ProxySQL configuration tables and config modules.

Column lists describe the ProxySQL 2.x/3.x schema. At runtime the real columns are
read from the server, so columns missing on older versions are hidden and columns
added by newer versions are still shown (as plain text fields).
"""

STATUS_CHOICES = ["ONLINE", "SHUNNED", "OFFLINE_SOFT", "OFFLINE_HARD"]


def c(name, type="text", default=None, nullable=False, required=False, choices=None,
      help=None, advanced=False, label=None):
    return {"name": name, "type": type, "default": default, "nullable": nullable,
            "required": required, "choices": choices, "help": help,
            "advanced": advanced, "label": label or name}


def flag(name, default=0, help=None, advanced=False):
    return c(name, "bool", default, help=help, advanced=advanced)


TABLES = {
    "mysql_servers": {
        "label": "Backend Servers",
        "module": "MYSQL SERVERS",
        "pk": ["hostgroup_id", "hostname", "port"],
        "order": "hostgroup_id, hostname, port",
        "columns": [
            c("hostgroup_id", "int", 0, required=True, help="Hostgroup this server belongs to"),
            c("hostname", required=True, help="Backend MariaDB/MySQL host or IP"),
            c("port", "int", 3306, required=True),
            c("gtid_port", "int", 0, advanced=True, help="ProxySQL Binlog Reader port (0 = disabled)"),
            c("status", "enum", "ONLINE", choices=STATUS_CHOICES),
            c("weight", "int", 1, help="Relative weight inside the hostgroup"),
            c("compression", "int", 0, advanced=True),
            c("max_connections", "int", 1000),
            c("max_replication_lag", "int", 0, help="Shun when lag exceeds N seconds (0 = disabled)"),
            flag("use_ssl", advanced=True),
            c("max_latency_ms", "int", 0, advanced=True),
            c("comment", default=""),
        ],
    },
    "mysql_replication_hostgroups": {
        "label": "Replication Hostgroups",
        "module": "MYSQL SERVERS",
        "pk": ["writer_hostgroup"],
        "order": "writer_hostgroup",
        "columns": [
            c("writer_hostgroup", "int", required=True),
            c("reader_hostgroup", "int", required=True),
            c("check_type", "enum", "read_only",
              choices=["read_only", "innodb_read_only", "super_read_only",
                       "read_only|innodb_read_only", "read_only&innodb_read_only"]),
            c("comment", default=""),
        ],
    },
    "mysql_galera_hostgroups": {
        "label": "Galera Hostgroups",
        "module": "MYSQL SERVERS",
        "pk": ["writer_hostgroup"],
        "order": "writer_hostgroup",
        "columns": [
            c("writer_hostgroup", "int", required=True),
            c("backup_writer_hostgroup", "int", required=True),
            c("reader_hostgroup", "int", required=True),
            c("offline_hostgroup", "int", required=True),
            flag("active", 1),
            c("max_writers", "int", 1),
            c("writer_is_also_reader", "enum", "0", choices=["0", "1", "2"],
              help="0 = no, 1 = yes, 2 = only backup writers are readers"),
            c("max_transactions_behind", "int", 0),
            c("comment", nullable=True),
        ],
    },
    "mysql_group_replication_hostgroups": {
        "label": "Group Replication Hostgroups",
        "module": "MYSQL SERVERS",
        "pk": ["writer_hostgroup"],
        "order": "writer_hostgroup",
        "columns": [
            c("writer_hostgroup", "int", required=True),
            c("backup_writer_hostgroup", "int", required=True),
            c("reader_hostgroup", "int", required=True),
            c("offline_hostgroup", "int", required=True),
            flag("active", 1),
            c("max_writers", "int", 1),
            flag("writer_is_also_reader", 0),
            c("max_transactions_behind", "int", 0),
            c("comment", nullable=True),
        ],
    },
    "mysql_users": {
        "label": "MySQL Users",
        "module": "MYSQL USERS",
        "pk": ["username", "backend", "frontend"],
        "order": "username",
        "secret": ["password"],
        "columns": [
            c("username", required=True),
            c("password", "password", "", help="Clear text or mysql_native_password hash (*HEX). "
                                                "Leave empty when editing to keep the current one."),
            flag("active", 1),
            flag("use_ssl", 0),
            c("default_hostgroup", "int", 0, required=True),
            c("default_schema", nullable=True),
            flag("schema_locked", 0, advanced=True),
            flag("transaction_persistent", 1),
            flag("fast_forward", 0, advanced=True),
            flag("backend", 1),
            flag("frontend", 1),
            c("max_connections", "int", 10000),
            c("attributes", "textarea", "", advanced=True, help="JSON attributes"),
            c("comment", default=""),
        ],
    },
    "mysql_query_rules": {
        "label": "Query Rules",
        "module": "MYSQL QUERY RULES",
        "pk": ["rule_id"],
        "order": "rule_id",
        "columns": [
            c("rule_id", "int", required=True, help="Rules are evaluated in rule_id order"),
            flag("active", 1),
            c("username", nullable=True),
            c("schemaname", nullable=True),
            c("flagIN", "int", 0, advanced=True),
            c("client_addr", nullable=True, advanced=True),
            c("proxy_addr", nullable=True, advanced=True),
            c("proxy_port", "int", nullable=True, advanced=True),
            c("digest", nullable=True, advanced=True),
            c("match_digest", nullable=True, help="Regex matched against the query digest"),
            c("match_pattern", nullable=True, help="Regex matched against the query text"),
            flag("negate_match_pattern", 0, advanced=True),
            c("re_modifiers", default="CASELESS", advanced=True),
            c("flagOUT", "int", nullable=True, advanced=True),
            c("replace_pattern", nullable=True, advanced=True),
            c("destination_hostgroup", "int", nullable=True),
            c("cache_ttl", "int", nullable=True, help="Milliseconds"),
            c("cache_empty_result", "int", nullable=True, advanced=True),
            c("cache_timeout", "int", nullable=True, advanced=True),
            c("reconnect", "int", nullable=True, advanced=True),
            c("timeout", "int", nullable=True, advanced=True),
            c("retries", "int", nullable=True, advanced=True),
            c("delay", "int", nullable=True, advanced=True),
            c("next_query_flagIN", "int", nullable=True, advanced=True),
            c("mirror_flagOUT", "int", nullable=True, advanced=True),
            c("mirror_hostgroup", "int", nullable=True, advanced=True),
            c("error_msg", nullable=True, advanced=True),
            c("OK_msg", nullable=True, advanced=True),
            c("sticky_conn", "int", nullable=True, advanced=True),
            c("multiplex", "int", nullable=True, advanced=True),
            c("gtid_from_hostgroup", "int", nullable=True, advanced=True),
            c("log", "int", nullable=True, advanced=True),
            c("apply", "bool", 0),
            c("attributes", "textarea", "", advanced=True),
            c("comment", nullable=True),
        ],
    },
    "proxysql_servers": {
        "label": "Cluster Nodes",
        "module": "PROXYSQL SERVERS",
        "pk": ["hostname", "port"],
        "order": "hostname, port",
        "columns": [
            c("hostname", required=True),
            c("port", "int", 6032, required=True),
            c("weight", "int", 0),
            c("comment", default=""),
        ],
    },
    "scheduler": {
        "label": "Scheduler",
        "module": "SCHEDULER",
        "pk": ["id"],
        "order": "id",
        "columns": [
            c("id", "int", required=True),
            flag("active", 1),
            c("interval_ms", "int", 10000, required=True),
            c("filename", required=True, help="Absolute path of the executable"),
            c("arg1", nullable=True), c("arg2", nullable=True), c("arg3", nullable=True),
            c("arg4", nullable=True), c("arg5", nullable=True),
            c("comment", default=""),
        ],
    },
}

# Config modules: what LOAD/SAVE commands operate on, and the tables they cover.
MODULES = [
    {"key": "MYSQL USERS", "tables": ["mysql_users"]},
    {"key": "MYSQL SERVERS", "tables": ["mysql_servers", "mysql_replication_hostgroups",
                                        "mysql_galera_hostgroups", "mysql_group_replication_hostgroups"]},
    {"key": "MYSQL QUERY RULES", "tables": ["mysql_query_rules"]},
    {"key": "MYSQL VARIABLES", "tables": ["global_variables"], "var_prefix": "mysql-"},
    {"key": "ADMIN VARIABLES", "tables": ["global_variables"], "var_prefix": "admin-"},
    {"key": "PROXYSQL SERVERS", "tables": ["proxysql_servers"]},
    {"key": "SCHEDULER", "tables": ["scheduler"]},
]
MODULE_KEYS = [m["key"] for m in MODULES]

ACTIONS = {
    "load_runtime": ("LOAD {m} TO RUNTIME", "Memory -> Runtime"),
    "save_disk": ("SAVE {m} TO DISK", "Memory -> Disk"),
    "load_disk": ("LOAD {m} FROM DISK", "Disk -> Memory"),
    "save_memory": ("SAVE {m} FROM RUNTIME", "Runtime -> Memory"),
    "load_config": ("LOAD {m} FROM CONFIG", "Config file -> Memory"),
}

# Columns ignored when comparing memory with runtime (runtime legitimately differs).
RUNTIME_IGNORE = {
    "mysql_servers": ["status"],
    # runtime splits frontend+backend users into two rows and may hash passwords
    "mysql_users": ["password", "frontend", "backend"],
}

VARIABLE_PREFIXES = ["mysql-", "admin-", "pgsql-", "ldap-", "sqliteserver-", "clickhouse-"]
VARIABLE_MODULE = {"mysql-": "MYSQL VARIABLES", "admin-": "ADMIN VARIABLES",
                   "pgsql-": "PGSQL VARIABLES", "ldap-": "LDAP VARIABLES",
                   "sqliteserver-": "SQLITESERVER VARIABLES", "clickhouse-": "CLICKHOUSE VARIABLES"}
