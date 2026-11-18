# fsync

Distribute a set of files to **running** Proxmox LXC containers via
rsync over SSH.

**Requires Proxmox 8.0+** (for the LXC status and interfaces APIs),
`rsync` on the host running the script, and passwordless SSH access to
each container (containers run their own sshd).

## Config file

Host, user, credentials, and file definitions live in a JSON config file.

- **Default path:** `~/.config/fsync/config.json`
- **Override:** `--config PATH` or the `FSYNC_CONFIG` env var

See [`config.example.json`](config.example.json) for a worked example.

Format:

```json
{
  "default_cluster": "home-lab",
  "clusters": {
    "home-lab": {
      "host": "pve1",
      "port": 8006,
      "user": "root@pam",
      "token_name": "fsync",
      "token_value": "abc123secret",
      "insecure": true,
      "ssh_user": "root",
      "ssh_key": "~/.ssh/id_ed25519"
    },
    "work": {
      "host": "pve9",
      "user": "admin@pve",
      "password": "s3cret",
      "insecure": true,
      "ssh_user": "deploy"
    }
  },
  "files": [
    {
      "source": "config/app.conf",
      "target": "/etc/app/app.conf",
      "ssh_user": "root",
      "ssh_key": "~/.ssh/id_ed25519"
    },
    {
      "source": "certs/ssl-cert.pem",
      "target": "/etc/ssl/certs/ssl-cert.pem",
      "include": ["100", "200"]
    },
    {
      "source": "scripts/cron.sh",
      "target": "/usr/local/bin/cron.sh",
      "exclude": ["500"]
    },
    {
      "source": "readme.txt",
      "target": "/var/www/html/readme.txt"
    }
  ]
}
```

### Cluster entries

Same fields as `bulk_command`: `host`, `port`, `user`, `token_name`,
`token_value`, `password`, `insecure`, `timeout`, `ssh_user`, `ssh_key`.

**Precedence per field:** CLI flag > named cluster (`--cluster NAME`) >
`default_cluster` > built-in default (`port=8006`, `ssh_user` = your
username, key auth via your standard `~/.ssh` keys).

> Store the file with `chmod 600` — it may contain your token secret or
> password.

**Self-signed API cert:** set `"insecure": true` on the cluster (or pass
`--insecure`) to skip verification.

### File entries

Each entry in the `files` list:

| Field | Required | Description |
|---|---|---|
| `source` | yes | Path to the file to sync, relative to the config file's directory (absolute paths used as-is) |
| `target` | yes | Destination path on the container |
| `ssh_user` | no | SSH user for this file's transfer (default: cluster `ssh_user`, then your username) |
| `ssh_key` | no | SSH private key path for this file (default: cluster `ssh_key`) |
| `include` | no | Only sync to these container names/vmids |
| `exclude` | no | Sync to all containers except these names/vmids |

`include` and `exclude` match on container name (primary) or vmid.
If both are given, `include` is applied first, then `exclude` from the
result.

## How it works

1. `GET /nodes` — list cluster nodes
2. `GET /nodes/{node}/lxc` — status list; only containers with
   `status == running` that are not templates are considered
3. `GET /nodes/{node}/lxc/{vmid}/interfaces` — read the `eth0` IP address
4. For each file in the config, apply its include/exclude filter to the
   target list
5. `rsync -a --no-perms -- -e 'ssh ... user@ip' <source> user@ip:<target>`
   — transfer the file. If the resolved IP is one of the local machine's
   own addresses (i.e. you're running the script from inside that very
   container), the rsync runs locally without the SSH hop.
6. No `--delete`: files are added/updated only; extra files on the target
   are never removed.

**Output convention:** by default, stdout carries only the per-container
sync result line. Diagnostic messages (`[info]`, `[scan]`, `[warn]`,
`[sync]`) are suppressed unless `--verbose` is passed; `[error]` messages
are always shown on stderr.

Exit code: `0` if every sync succeeded, `1` if any failed, `2` on
connection/authentication/discovery errors.

## Usage

```bash
# Sync all configured files to all running containers (uses default_cluster)
python3 fsync.py

# Use a specific cluster from the config
python3 fsync.py --cluster work

# Dry run: print the rsync commands without executing them
python3 fsync.py --dry-run

# One-off override without touching the config
python3 fsync.py --host pve2 --user 'admin@pve' --password SECRET

# Run as a specific SSH user (all files)
python3 fsync.py --ssh-user deploy

# Show all diagnostic output
python3 fsync.py --verbose
```

## Options

| Option | Description |
|---|---|
| `--config` | Path to the JSON config file (default `~/.config/fsync/config.json`) |
| `--cluster` | Name of a cluster defined in the config (overrides `default_cluster`) |
| `--host` | Proxmox cluster hostname or IP (overrides config) |
| `--port` | API port (default `8006`) |
| `--user` | PVE user, e.g. `root@pam` (overrides config) |
| `--token-name` | API token key name (only needed if not in `--user`) |
| `--token-value` | API token secret (overrides config) |
| `--password` | Password auth (used when no token given) |
| `--insecure` | Skip SSL certificate verification |
| `--timeout` | Per-file rsync/SSH timeout in seconds (default `60`) |
| `--ssh-user` | SSH user for all rsync transfers (default: your username) |
| `--ssh-key` | SSH private key path for all transfers (default: your standard `~/.ssh` keys) |
| `--dry-run` | Print rsync commands without executing them |
| `--verbose` | Show all diagnostic output on stderr |

## Notes

- The container must already be **running** — the script does not start
  containers.
- The PVE API user needs read permission on the target LXC vmids (to list
  them and read their interfaces). Your SSH key (or `ssh_user` login)
  must be authorized on every container's sshd for the users specified.
- `--no-perms` keeps file permissions from the source; no `--delete` is
  ever passed — only add/update, never remove.
- Source paths are resolved relative to the config file's directory,
  so the config and its files can live anywhere on disk.

## License

MIT
