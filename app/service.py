"""Local ProxySQL systemd service control and log access.

Privileged commands go through sudo with fixed argument lists that match the
sudoers drop-in installed by deploy/install.sh exactly.
"""
import subprocess

LOG_LINES = "500"

SHOW_PROPS = ["ActiveState", "SubState", "LoadState", "UnitFileState", "MainPID",
              "ActiveEnterTimestamp", "ExecMainStartTimestamp", "NRestarts",
              "MemoryCurrent", "TasksCurrent", "Description", "FragmentPath"]


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, universal_newlines=True)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out: %s" % " ".join(cmd)


def _priv(cfg, cmd):
    return (["/usr/bin/sudo", "-n"] + cmd) if cfg.use_sudo else cmd


def status(cfg):
    rc, out, err = _run([cfg.systemctl, "show", cfg.service_unit, "--no-pager",
                         "--property=" + ",".join(SHOW_PROPS)], timeout=15)
    props = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    if rc != 0 and not props:
        return {"available": False, "error": err.strip() or "systemctl failed (rc=%s)" % rc}
    props["available"] = True
    props["unit"] = cfg.service_unit
    return props


def control(cfg, action):
    if action not in ("start", "stop", "restart"):
        raise ValueError("Unsupported action")
    rc, out, err = _run(_priv(cfg, [cfg.systemctl, action, cfg.service_unit]), timeout=120)
    return {"ok": rc == 0, "rc": rc, "output": (out + err).strip()}


def logs(cfg, source):
    if source == "journal":
        cmd = [cfg.journalctl, "-u", cfg.service_unit, "--no-pager", "-n", LOG_LINES, "-o", "short-iso"]
        rc, out, err = _run(cmd, timeout=30)
        if rc != 0 or not out.strip() or "No journal files" in out:
            # fall back to sudo if the account is not in the systemd-journal group
            rc2, out2, err2 = _run(_priv(cfg, cmd), timeout=30)
            if rc2 == 0:
                return {"ok": True, "text": out2}
            return {"ok": rc == 0, "text": out, "error": (err or err2).strip()}
        return {"ok": True, "text": out}
    # ProxySQL's own log file
    try:
        with open(cfg.service_log_file, "r", errors="replace") as fh:
            lines = fh.readlines()[-int(LOG_LINES):]
        return {"ok": True, "text": "".join(lines)}
    except (IOError, OSError) as e:
        rc, out, err = _run(_priv(cfg, [cfg.tail, "-n", LOG_LINES, cfg.service_log_file]), timeout=30)
        if rc == 0:
            return {"ok": True, "text": out}
        return {"ok": False, "text": "", "error": "%s; sudo tail: %s" % (e, err.strip())}
