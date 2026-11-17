#!/usr/bin/env python3
"""Manage Utter keys on the server (run as root). The server picks up every change at once, no restart needed.

  utter-keys add <name>      make a key for <name> and print it ONCE (only its hash is kept)
  utter-keys list            names, when made, and whether revoked
  utter-keys revoke <name>   that key stops working at the next connection
"""
import hashlib, json, os, secrets, sys, time

KEYS_FILE = "/var/lib/private/utter-server/keys.json"  # the service's private state directory (0700)


def load() -> dict:
    try:
        with open(KEYS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(keys: dict) -> None:
    tmp = KEYS_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), "w") as f:  # the 0700 folder guards it
        json.dump(keys, f, indent=2)
    os.replace(tmp, KEYS_FILE)  # atomic: the server never reads half a file


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    if not os.path.isdir(os.path.dirname(KEYS_FILE)):
        sys.exit(f"{os.path.dirname(KEYS_FILE)} is missing: start utter-server once first.")
    cmd, *args = sys.argv[1:] or ["help"]
    keys = load()
    names = {v["name"]: h for h, v in keys.items()}
    if cmd == "add" and len(args) == 1:
        name = args[0]
        if name in names and not keys[names[name]].get("revoked"):
            sys.exit(f"{name} already has a working key. Revoke it first to make a new one.")
        key = "utr_" + secrets.token_urlsafe(32)
        keys[hashlib.sha256(key.encode()).hexdigest()] = {
            "name": name, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "revoked": False}
        save(keys)
        print(f"Utter key for {name} (shown once, keep it safe):\n\n  {key}\n")
    elif cmd == "list":
        for v in keys.values():
            print(f"{v['name']:<20} {v['created']}  {'REVOKED ' + v.get('revoked_at', '') if v.get('revoked') else 'active'}")
        if not keys:
            print("No keys yet.")
    elif cmd == "revoke" and len(args) == 1:
        live = [h for h, v in keys.items() if v["name"] == args[0] and not v.get("revoked")]
        if not live:
            sys.exit(f"No working key for {args[0]}.")
        for h in live:
            keys[h].update(revoked=True, revoked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        save(keys)
        print(f"Revoked {args[0]}'s key.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
