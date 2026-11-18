#!/usr/bin/env python3
"""
fsync.py — distribute a set of files to LXC containers in a Proxmox cluster
via rsync over SSH.

Discovery: Proxmox REST API (same flow as bulk_command.py).
Transfer: rsync (no --delete) over SSH to each container's eth0 IP.
Config: JSON file with cluster credentials + per-file definitions.

Usage:
  fsync.py [--config PATH] [--cluster NAME] [--dry-run] [--verbose]
"""

import argparse
import getpass
import json
import os
import pwd
import subprocess
import sys
import time
import urllib3
import warnings

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings()
warnings.filterwarnings('ignore', category=requests.exceptions.RequestsWarning)

_session = requests.Session()
_session.mount('https://',
               HTTPAdapter(max_retries=Retry(connect=3, read=0, backoff_factor=0.5)))

_ticket_cache = {}
_TICKET_TTL = 3600

_verbose = False


def log(msg):
    """Diagnostic output — stderr only."""
    if _verbose or msg.startswith('[error]'):
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Cluster config
# ---------------------------------------------------------------------------

CONFIG_PATH_DEFAULT = os.path.expanduser('~/.config/fsync/config.json')

BUILTIN_DEFAULTS = {
    'host': None,
    'port': 8006,
    'user': None,
    'token_name': None,
    'token_value': None,
    'password': None,
    'insecure': False,
    'timeout': 60,
    'ssh_user': getpass.getuser(),
    'ssh_key': None,
}


def find_config_file(explicit_path):
    """Resolve the config file path: --config flag > env var > default."""
    if explicit_path:
        return explicit_path
    if os.environ.get('FSYNC_CONFIG'):
        return os.environ['FSYNC_CONFIG']
    return CONFIG_PATH_DEFAULT


def load_config(path):
    """Load and validate the config file.

    Returns (config_dict, default_cluster_name).
    The config must have a "clusters" object. A "files" list is optional.
    """
    if not os.path.exists(path):
        log(f'[warn] config file {path} not found')
        return {}, None

    try:
        with open(path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f'[error] cannot read config file {path}: {e}')
        sys.exit(2)

    clusters = config.get('clusters')
    if not isinstance(clusters, dict) or not clusters:
        log(f'[error] config file {path} has no "clusters" object')
        sys.exit(2)

    for name, c in clusters.items():
        if not isinstance(c, dict):
            log(f'[error] cluster "{name}" in config must be an object')
            sys.exit(2)
        if not c.get('host') or not c.get('user'):
            log(f'[error] cluster "{name}" needs at least "host" and "user"')
            sys.exit(2)

    default_name = config.get('default_cluster')
    if default_name and default_name not in clusters:
        log(f'[error] default_cluster "{default_name}" not found in clusters')
        sys.exit(2)

    # Validate the files list if present.
    files = config.get('files')
    if files is not None:
        if not isinstance(files, list):
            log(f'[error] "files" in config must be a list')
            sys.exit(2)
        for i, f in enumerate(files):
            if not isinstance(f, dict):
                log(f'[error] files[{i}] must be an object')
                sys.exit(2)
            if not f.get('source'):
                log(f'[error] files[{i}] needs a "source" field')
                sys.exit(2)
            if not f.get('target'):
                log(f'[error] files[{i}] needs a "target" field')
                sys.exit(2)

    return config, default_name


def resolve_cluster_config(args, config, default_name):
    """Merge config + CLI into a single args object with final values.

    Precedence per field: CLI flag > named cluster > default_cluster > builtin.
    """
    cluster_entry = {}
    if args.cluster:
        if args.cluster not in config.get('clusters', {}):
            log(f'[error] --cluster "{args.cluster}" not found in config '
                f'(available: {", ".join(config.get("clusters", {}))})')
            sys.exit(2)
        cluster_entry = config['clusters'][args.cluster]
        log(f'[info] using cluster "{args.cluster}" from config')
    elif default_name:
        cluster_entry = config['clusters'][default_name]
        log(f'[info] using default cluster "{default_name}"')

    def pick(cli_value, cfg_value, builtin_value):
        if cli_value is not None:
            return cli_value
        if cfg_value is not None:
            return cfg_value
        return builtin_value

    args.host = pick(args.host, cluster_entry.get('host'), BUILTIN_DEFAULTS['host'])
    args.port = pick(args.port, cluster_entry.get('port'), BUILTIN_DEFAULTS['port'])
    args.user = pick(args.user, cluster_entry.get('user'), BUILTIN_DEFAULTS['user'])
    args.token_name = pick(args.token_name, cluster_entry.get('token_name'),
                           BUILTIN_DEFAULTS['token_name'])
    args.token_value = pick(args.token_value, cluster_entry.get('token_value'),
                            BUILTIN_DEFAULTS['token_value'])
    args.password = pick(args.password, cluster_entry.get('password'),
                         BUILTIN_DEFAULTS['password'])
    if not args.insecure and cluster_entry.get('insecure'):
        args.insecure = True
    args.timeout = pick(args.timeout, cluster_entry.get('timeout'),
                        BUILTIN_DEFAULTS['timeout'])
    args.ssh_user = pick(args.ssh_user, cluster_entry.get('ssh_user'),
                         BUILTIN_DEFAULTS['ssh_user'])
    args.ssh_key = pick(args.ssh_key, cluster_entry.get('ssh_key'),
                        BUILTIN_DEFAULTS['ssh_key'])
    if args.ssh_key:
        args.ssh_key = os.path.expanduser(args.ssh_key)

    if not args.host:
        log('[error] no cluster host configured — set it in the config file '
            'or pass --host')
        sys.exit(2)
    if not args.user:
        log('[error] no cluster user configured — set it in the config file '
            'or pass --user')
        sys.exit(2)
    if not args.token_value and not args.password:
        log('[error] no credentials — set token_value or password in the '
            'config file, or pass --token-value / --password')


# ---------------------------------------------------------------------------
# Auth helpers (same as bulk_command.py)
# ---------------------------------------------------------------------------


def _parse_token_user(user_string):
    """Parse 'user@realm!keyname' — also strips bash-escaped \\! artifacts."""
    cleaned = user_string.replace('\\!', '!').replace('\\\\', '\\')
    if '!' in cleaned:
        parts = cleaned.rsplit('!', 1)
        return f'{parts[0]}!{parts[1]}'
    return cleaned


def _build_api_token_header(service, username, token_name, token_value):
    """Build the PVEAPIToken Authorization header value."""
    clean_username = username.replace('\\!', '!').replace('\\\\', '\\')
    clean_token_value = (token_value or '').replace('\\!', '!').replace('\\\\', '\\')
    if token_name and '!' not in clean_username:
        return f'{service}APIToken={clean_username}!{token_name}={clean_token_value}'
    return f'{service}APIToken={clean_username}={clean_token_value}'


def _auth_headers_cookies(args, user, token_name, token_value):
    """Return (headers, cookies) for one API request."""
    headers = {'Accept': 'application/json'}
    cookies = None
    if token_value:
        headers['Authorization'] = _build_api_token_header(
            'PVE', user, token_name, token_value)
    elif args.password:
        cache_key = (args.base_url, user)
        cached = _ticket_cache.get(cache_key)
        if cached and time.time() - cached[1] < _TICKET_TTL:
            pve_ticket = cached[0]
        else:
            r = _session.post(f'{args.base_url}/api2/json/access/ticket',
                              data={'username': user, 'password': args.password},
                              verify=not args.insecure, timeout=10)
            r.raise_for_status()
            ticket_data = r.json().get('data') or {}
            pve_ticket = ticket_data.get('ticket') or ''
            if not pve_ticket:
                raise PermissionError('No ticket returned from /access/ticket')
            _ticket_cache[cache_key] = (pve_ticket, time.time())
        cookies = {'PVEAuthCookie': pve_ticket}
    return headers, cookies


def api_request(args, user, token_name, token_value, method, path,
                params=None, timeout=15):
    """GET/PUT helper. Returns the 'data' field of the JSON response."""
    url = f'{args.base_url}/api2/json{path}'
    headers, cookies = _auth_headers_cookies(args, user, token_name, token_value)
    r = _session.request(method, url, params=params, headers=headers,
                         cookies=cookies, verify=not args.insecure,
                         timeout=timeout)
    if r.status_code == 401:
        _ticket_cache.pop((args.base_url, user), None)
        raise PermissionError(f'Authentication failed for {path}')
    if r.status_code >= 400:
        detail = ''
        try:
            err = r.json()
            detail = err.get('errors') or str(err)
        except Exception:
            detail = r.text
        raise RuntimeError(f'API error {r.status_code} for {path}: {detail}')
    r.raise_for_status()
    return r.json().get('data')


# ---------------------------------------------------------------------------
# Discovery (same flow as bulk_command.py)
# ---------------------------------------------------------------------------


def get_container_ip(args, user, token_name, token_value, node, vmid):
    """Get the container's primary IPv4 address via the Proxmox interfaces
    endpoint. Returns the IP string, or None if not found.
    """
    data = api_request(args, user, token_name, token_value,
                       'GET', f'/nodes/{node}/lxc/{vmid}/interfaces',
                       timeout=10)
    if not isinstance(data, list):
        return None
    for iface in data:
        if iface.get('name') == 'eth0':
            inet = iface.get('inet', '')
            if inet:
                return inet.split('/')[0]
    return None


def local_ipv4_addrs():
    """IPv4 addresses of the local machine's interfaces.

    Used to detect when a target container is this machine itself
    (e.g. running the script from inside one of the cluster's own
    containers): in that case the sync runs locally, not over SSH.
    """
    try:
        proc = subprocess.run(['ip', '-4', '-o', 'addr', 'show'],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            addrs = set()
            for line in proc.stdout.splitlines():
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == 'inet' and i + 1 < len(parts):
                        addrs.add(parts[i + 1].split('/')[0])
            return addrs
    except Exception:
        pass
    return set()


def discover_containers(args, user, token_name, token_value):
    """Discover all running, non-template LXC containers across the cluster.

    Returns a list of dicts: {name, vmid, node, ip}.
    """
    try:
        nodes = api_request(args, user, token_name, token_value,
                            'GET', '/nodes', timeout=15)
    except Exception as e:
        log(f'[error] cannot reach Proxmox at {args.base_url} as {user}: {e}')
        sys.exit(2)
    if not isinstance(nodes, list) or not nodes:
        log('[error] no nodes returned — check credentials/permissions')
        sys.exit(2)

    log(f'[info] {len(nodes)} node(s): {[n["node"] for n in nodes]}')

    targets = []
    for node_info in nodes:
        node = node_info.get('node')
        try:
            lxc_list = api_request(args, user, token_name, token_value,
                                  'GET', f'/nodes/{node}/lxc', timeout=15)
        except Exception as e:
            log(f'[warn] node {node}: {e}')
            continue
        for entry in lxc_list if isinstance(lxc_list, list) else []:
            if entry.get('type') != 'lxc' or entry.get('template') == 1:
                continue
            if entry.get('status') != 'running':
                continue
            vmid = str(entry.get('vmid'))
            name = entry.get('name', f'unknown-{vmid}')
            log(f'[scan] {name} (vmid={vmid}) on {node}')
            targets.append({'name': name, 'vmid': vmid, 'node': node, 'ip': None})

    # Resolve IPs.
    for t in targets:
        try:
            t['ip'] = get_container_ip(args, user, token_name, token_value,
                                     t['node'], t['vmid'])
        except Exception as e:
            t['ip'] = None
            log(f'[warn] {t["name"]} ({t["vmid"]}): cannot read interfaces: {e}')
        if not t['ip']:
            log(f'[warn] {t["name"]} ({t["vmid"]}): no IP address found')

    if any(t['ip'] is None for t in targets):
        bad = [t['name'] for t in targets if t['ip'] is None]
        log(f'[error] {len(bad)}/{len(targets)} container(s) have no IP address '
            f'({", ".join(bad)}) — cannot sync to them')
        sys.exit(2)

    return targets


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------


def filter_targets(targets, include=None, exclude=None):
    """Apply include/exclude filtering to the container list.

    include: only these container names/vmids are in scope.
    exclude: start with all, remove these names/vmids.
    Both match on name (primary) and vmid.
    """
    if include:
        wanted = set(include)
        targets = [t for t in targets
                   if t['name'] in wanted or t['vmid'] in wanted]
    if exclude:
        excluded = set(exclude)
        targets = [t for t in targets
                   if t['name'] not in excluded and t['vmid'] not in excluded]
    return targets


def _ssh_args_base(ssh_user, ssh_key, host):
    """SSH options list (no user@host — rsync gets that from the destination)."""
    a = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no',
         '-o', 'ConnectTimeout=10', '-p22']
    if ssh_key:
        a.extend(['-i', ssh_key])
    return a


def resolve_source(config_dir, source, config_base_dir):
    """Resolve a source path relative to the config file's directory.

    If `source` is absolute, use it as-is.
    If `source` starts with `base_dir` (the directory of the config file),
    resolve relative to that directory.
    Otherwise, resolve relative to config_dir (cwd).
    """
    if source.startswith('/'):
        return source
    abs_source = os.path.abspath(os.path.join(config_dir, source))
    return abs_source


def sync_file(config_dir, file_entry, target, timeout, dry_run=False):
    """Sync one source file to one container via rsync.

    Returns (ok: bool, status: str).
    """
    ssh_user = file_entry.get('ssh_user') or getpass.getuser()
    ssh_key = file_entry.get('ssh_key')
    if ssh_key:
        ssh_key = os.path.expanduser(ssh_key)

    # Resolve source path relative to config file's directory.
    source = resolve_source(config_dir, file_entry['source'],
                            os.path.dirname(os.path.abspath(config_dir)))
    target_path = file_entry['target']

    # Check source exists.
    if not os.path.exists(source):
        return False, f'source not found: {source}'

    # Build rsync command.
    if target['ip'] in local_ipv4_addrs():
        # Local target: no SSH hop.
        cmd = ['rsync', '-a', '--no-perms', source, target_path]
    else:
        cmd = ['rsync', '-a', '--no-perms', '--mkpath',
               '-e', ' '.join(_ssh_args_base(ssh_user, ssh_key, target['ip']))]
        cmd.append(source)
        cmd.append(f'{ssh_user}@{target["ip"]}:{target_path}')

    if dry_run:
        return True, f'dry-run: {" ".join(cmd)}'

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode != 0:
            return False, f'rsync exit {proc.returncode}: {proc.stderr.strip()}'
        return True, 'ok'
    except subprocess.TimeoutExpired:
        return False, f'rsync timed out after {timeout}s'


def process_files(args, config, targets, dry_run=False):
    """Process each file entry in the config against the (filtered) targets.

    Returns the number of failed syncs.
    """
    files = config.get('files', [])
    if not files:
        log('[info] no files defined in config — nothing to sync')
        return 0

    config_dir = find_config_file(args.config)
    failed = 0

    for file_entry in files:
        include = file_entry.get('include')
        exclude = file_entry.get('exclude')
        scope = filter_targets(targets, include, exclude)
        if not scope:
            log(f'[warn] {file_entry["source"]}: no targets after filtering')
            continue
        log(f'[sync] {file_entry["source"]} -> {len(scope)} target(s)')
        for t in scope:
            ok, status = sync_file(config_dir, file_entry, t,
                                   args.timeout, dry_run)
            tag = '[dry]' if dry_run else ''
            log(f'  {tag}{t["name"]}: {status}')
            if not ok:
                failed += 1

    return failed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description='Distribute files to LXC containers in a Proxmox cluster.')
    parser.add_argument('--config',
                         help='Path to config file '
                              f'(default: {CONFIG_PATH_DEFAULT})')
    parser.add_argument('--cluster',
                         help='Cluster name from config')
    parser.add_argument('--host', help='Proxmox API host')
    parser.add_argument('--port', type=int, help='Proxmox API port')
    parser.add_argument('--user', help='Proxmox API user')
    parser.add_argument('--token-name', help='API token name')
    parser.add_argument('--token-value', help='API token value')
    parser.add_argument('--password', help='API password (alternative to token)')
    parser.add_argument('--insecure', action='store_true',
                         help='Allow self-signed certificates')
    parser.add_argument('--timeout', type=int, help='rsync/SSH timeout (seconds)')
    parser.add_argument('--ssh-user', help='SSH user for rsync (default: current user)')
    parser.add_argument('--ssh-key', help='SSH private key path')
    parser.add_argument('--dry-run', action='store_true',
                         help='Print rsync commands without executing them')
    parser.add_argument('--verbose', action='store_true',
                         help='Show all diagnostic output')
    args = parser.parse_args()
    args.base_url = None  # set in main
    return args


def main():
    global _verbose
    args = parse_args()
    _verbose = args.verbose

    # Load config.
    config, default_name = load_config(find_config_file(args.config))
    resolve_cluster_config(args, config, default_name)
    args.base_url = f'https://{args.host}:{args.port}'

    # Discover containers.
    targets = discover_containers(args, args.user, args.token_name,
                                  args.token_value)
    log(f'[info] {len(targets)} running container(s) discovered')

    # Process files.
    failed = process_files(args, config, targets, dry_run=args.dry_run)

    if failed:
        log(f'[error] {failed} sync(s) failed')
        sys.exit(1)
    log('[info] all syncs completed')


if __name__ == '__main__':
    main()
