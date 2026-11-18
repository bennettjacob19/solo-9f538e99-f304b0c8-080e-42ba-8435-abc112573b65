"""Zone text parsing, whole-version validation, immutable snapshots, and
single-version resolution for the svc.test. authoritative zone."""

import ipaddress
import re
import threading

ZONE = "svc.test."
DEFAULT_TTL = 60
MAX_TTL = 2_147_483_647

_LABEL_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


class ZoneError(Exception):
    """Raised when a candidate zone version fails validation.

    Carries the full list of human-readable problems; the whole version
    is rejected, nothing is published.
    """

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def fqdn(name):
    """Normalize an operator-supplied name to an in-zone FQDN.

    Accepts relative names ("web"), zone FQDNs ("web.svc.test."), and the
    zone apex. Rejects anything outside the zone.
    """
    n = (name or "").strip().lower().rstrip(".")
    if not n:
        raise ValueError("empty name")
    if n == ZONE[:-1]:
        return ZONE
    if n.endswith("." + ZONE[:-1]):
        candidate = n + "."
    elif "." in n:
        raise ValueError(f"name '{name}' is outside zone {ZONE}")
    else:
        candidate = n + "." + ZONE
    _check_name(candidate)
    return candidate


def _check_name(name):
    if len(name) > 253:
        raise ValueError(f"name too long: {name}")
    for label in name.rstrip(".").split("."):
        if not _LABEL_RE.match(label):
            raise ValueError(f"bad label '{label}' in {name}")


def parse_zone_text(text):
    """Parse and validate a full zone version.

    Line format:  <name> <A|CNAME> <value> [ttl]     (# starts a comment)

    Returns {name: {"A": [ip, ...], "CNAME": target|None, "ttl": int}}.
    Raises ZoneError listing every problem found; nothing is half-accepted.
    """
    records = {}
    errors = []

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            errors.append(f"line {lineno}: expected '<name> <A|CNAME> <value> [ttl]'")
            continue
        if len(parts) > 4:
            errors.append(f"line {lineno}: too many fields")
            continue
        name_s, rtype, value = parts[0], parts[1].upper(), parts[2]
        ttl = DEFAULT_TTL
        if len(parts) == 4:
            try:
                ttl = int(parts[3])
                if not (0 < ttl <= MAX_TTL):
                    raise ValueError
            except ValueError:
                errors.append(f"line {lineno}: bad ttl '{parts[3]}'")
                continue
        try:
            name = fqdn(name_s)
        except ValueError as exc:
            errors.append(f"line {lineno}: {exc}")
            continue
        rec = records.setdefault(name, {"A": [], "CNAME": None, "ttl": ttl})

        if rtype == "A":
            try:
                ip = str(ipaddress.IPv4Address(value))
            except ValueError:
                errors.append(f"line {lineno}: invalid IPv4 address '{value}'")
                continue
            if ip not in rec["A"]:
                rec["A"].append(ip)
        elif rtype == "CNAME":
            try:
                target = fqdn(value)
            except ValueError:
                errors.append(
                    f"line {lineno}: CNAME target '{value}' is not a name inside {ZONE}"
                )
                continue
            if rec["CNAME"] is not None:
                errors.append(f"line {lineno}: duplicate CNAME for {name}")
                continue
            rec["CNAME"] = target
        else:
            errors.append(f"line {lineno}: unsupported record type '{parts[1]}' (A/CNAME only)")

    errors.extend(validate_records(records))
    if errors:
        raise ZoneError(errors)
    return records


def validate_records(records):
    """Cross-record rules for one candidate version.

    - a name cannot hold both A and CNAME
    - every CNAME target must exist in this same version (no dangling)
    - CNAME chains must be acyclic and terminate at a name with A record(s)
    """
    errors = []
    for name in sorted(records):
        rec = records[name]
        if rec["A"] and rec["CNAME"]:
            errors.append(f"{name}: cannot have both A and CNAME records")

    for name in sorted(records):
        rec = records[name]
        if not rec["CNAME"]:
            continue
        seen = {name}
        node = rec["CNAME"]
        while True:
            target = records.get(node)
            if target is None:
                errors.append(
                    f"{name}: CNAME target {node} is not defined in this version (dangling)"
                )
                break
            if node in seen:
                errors.append(f"{name}: CNAME chain loops back to {node} (cycle)")
                break
            seen.add(node)
            if target["CNAME"]:
                node = target["CNAME"]
                continue
            if target["A"]:
                break  # chain ends at address records: OK
            break  # pragma: no cover - a stored record always has A or CNAME
    return errors


def flatten_records(records):
    """parsed-dict -> list of (name, rtype, value, ttl) rows for storage."""
    rows = []
    for name in sorted(records):
        rec = records[name]
        for ip in rec["A"]:
            rows.append((name, "A", ip, rec["ttl"]))
        if rec["CNAME"]:
            rows.append((name, "CNAME", rec["CNAME"], rec["ttl"]))
    return rows


class Snapshot:
    """Immutable view of one published version. A query resolves its whole
    alias chain against the single Snapshot instance captured at query start,
    so the chain and the final A address always come from the same version."""

    __slots__ = ("version_id", "created_at", "records")

    def __init__(self, version_id, created_at, records):
        self.version_id = version_id
        self.created_at = created_at
        # freeze: A lists become tuples, structure is never mutated afterwards
        self.records = {
            name: {"A": tuple(rec["A"]), "CNAME": rec["CNAME"], "ttl": rec["ttl"]}
            for name, rec in records.items()
        }

    def zone_text(self):
        lines = []
        for name in sorted(self.records):
            rec = self.records[name]
            for ip in rec["A"]:
                lines.append(f"{name} A {ip} {rec['ttl']}")
            if rec["CNAME"]:
                lines.append(f"{name} CNAME {rec['CNAME']} {rec['ttl']}")
        return "\n".join(lines)


def snapshot_from_rows(version_id, created_at, rows):
    """Rebuild a Snapshot from stored rows (name, rtype, value, ttl)."""
    records = {}
    for name, rtype, value, ttl in rows:
        rec = records.setdefault(name, {"A": [], "CNAME": None, "ttl": ttl})
        if rtype == "A":
            rec["A"].append(value)
        else:
            rec["CNAME"] = value
    return Snapshot(version_id, created_at, records)


class SnapshotHolder:
    """Atomically-swapped reference to the current Snapshot.

    Publishing swaps the reference only after the SQLite transaction has
    committed; in-flight queries keep using the snapshot they captured, so
    service never pauses and never mixes versions."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snap = None

    def get(self):
        with self._lock:
            return self._snap

    def set(self, snap):
        with self._lock:
            self._snap = snap


def resolve(snapshot, qname, qtype):
    """Resolve qname/qtype against one snapshot.

    Returns (rcode, answers) where answers is a list of
    (name, rtype, value, ttl). qtype is a mnemonic: A, CNAME, ANY, ...
    """
    qname = qname.lower()
    if qname != ZONE and not qname.endswith("." + ZONE):
        return "REFUSED", []

    answers = []
    node = qname
    seen = set()
    while True:
        rec = snapshot.records.get(node)
        if rec is None:
            if answers:
                # A validated version never dangles; degrade gracefully.
                return "NOERROR", answers
            if node == ZONE:
                return "NOERROR", []  # zone apex exists but holds no such data
            return "NXDOMAIN", []
        if rec["CNAME"]:
            if node in seen:  # corrupt data safeguard; validated zones never loop
                return "NOERROR", answers
            seen.add(node)
            answers.append((node, "CNAME", rec["CNAME"], rec["ttl"]))
            if qtype == "CNAME":
                return "NOERROR", answers
            node = rec["CNAME"]
            continue
        if rec["A"]:
            if qtype in ("A", "ANY"):
                answers.extend((node, "A", ip, rec["ttl"]) for ip in rec["A"])
            return "NOERROR", answers
        return "NOERROR", answers  # pragma: no cover
