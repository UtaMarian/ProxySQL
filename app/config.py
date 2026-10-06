"""Configuration loading (INI file)."""
import configparser
import os
import secrets

DEFAULT_CONFIG_PATH = "/etc/proxysql-webui/config.ini"


class Instance(object):
    def __init__(self, key, section):
        self.key = key
        self.label = section.get("label", key)
        self.host = section.get("host", "127.0.0.1")
        self.port = section.getint("port", 6032)
        self.user = section.get("user", "admin")
        self.password = section.get("password", "admin")
        self.connect_timeout = section.getint("connect_timeout", 5)
        self.read_timeout = section.getint("read_timeout", 30)
        # "local" instances run on this host: service control & logs apply to them
        self.local = section.getboolean("local", False)

    def public(self):
        return {"key": self.key, "label": self.label, "host": self.host,
                "port": self.port, "local": self.local}


class Config(object):
    def __init__(self, path=None):
        self.path = path or os.environ.get("PROXYSQL_WEBUI_CONFIG", DEFAULT_CONFIG_PATH)
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(self.path):
            raise RuntimeError("Cannot read configuration file %s" % self.path)

        w = cp["webui"] if cp.has_section("webui") else {}
        self.secret_key = w.get("secret_key") or secrets.token_hex(32)
        self.data_dir = w.get("data_dir", "/var/lib/proxysql-webui")
        self.session_minutes = int(w.get("session_lifetime_minutes", 480))
        self.secure_cookies = str(w.get("secure_cookies", "true")).lower() in ("1", "true", "yes", "on")
        self.max_login_failures = int(w.get("max_login_failures", 5))
        self.lockout_minutes = int(w.get("lockout_minutes", 15))
        self.behind_proxy = str(w.get("behind_proxy", "false")).lower() in ("1", "true", "yes", "on")

        s = cp["service"] if cp.has_section("service") else {}
        self.service_enabled = str(s.get("enabled", "true")).lower() in ("1", "true", "yes", "on")
        self.service_unit = s.get("unit", "proxysql")
        self.service_log_file = s.get("log_file", "/var/lib/proxysql/proxysql.log")
        self.use_sudo = str(s.get("use_sudo", "true")).lower() in ("1", "true", "yes", "on")
        self.systemctl = s.get("systemctl", "/usr/bin/systemctl")
        self.journalctl = s.get("journalctl", "/usr/bin/journalctl")
        self.tail = s.get("tail", "/usr/bin/tail")

        self.instances = {}
        for name in cp.sections():
            if name.startswith("instance:"):
                key = name.split(":", 1)[1].strip()
                self.instances[key] = Instance(key, cp[name])
        if not self.instances:
            raise RuntimeError("No [instance:NAME] section defined in %s" % self.path)

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "webui.db")
