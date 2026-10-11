#!/usr/bin/env python3
"""Install/control standalone Osmocom containers on Ubuntu 26.04; no Observer."""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

SCRIPT = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location('osmocom_native', SCRIPT.with_name('install-osmocom-ubuntu2604.py'))
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
run = core.run

DEFAULT_DIRECTORY = Path('/opt/osmocom-lab-docker')
SNAP_COMMON = Path('/var/snap/docker/common')
SNAP_DIRECTORY = SNAP_COMMON / 'osmocom-lab-docker'
BOOT_UNIT = Path('/etc/systemd/system/osmocom-docker-lab.service')
BACKUPS = Path('/var/backups/osmocom-lab-docker')
DEPLOYMENT_FILES = ('compose.yaml', 'Dockerfile', 'entrypoint.sh',
                    'install-osmocom-docker.py', 'install-osmocom-ubuntu2604.py')


def docker_is_snap():
    """Detect the CLI launcher before invoking its confined filesystem view."""
    executable = shutil.which('docker')
    if not executable:
        return False
    path = Path(executable)
    return (str(path) == '/snap/bin/docker'
            or str(path).startswith('/snap/docker/')
            or path.resolve() == Path('/usr/bin/snap'))


def default_directory(command):
    # Installed helpers should use their own deployment, including custom paths.
    if command != 'render' and (SCRIPT.parent / 'config/settings.json').is_file():
        return SCRIPT.parent
    return SNAP_DIRECTORY if command != 'render' and docker_is_snap() else DEFAULT_DIRECTORY


def validate_directory(directory):
    import re
    if not directory.is_absolute() or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(directory)):
        raise ValueError('Use an absolute deployment path without spaces or shell metacharacters')
    if docker_is_snap() and (directory == SNAP_COMMON or not directory.is_relative_to(SNAP_COMMON)):
        raise ValueError(f'Snap Docker cannot reliably access this deployment path: {directory}. '
                         f'Use --directory {SNAP_DIRECTORY}; no files were moved automatically')

DOCKERFILE = '''FROM ubuntu:26.04
ARG CHANNEL=nightly
ENV DEBIAN_FRONTEND=noninteractive
RUN case "$CHANNEL" in nightly|latest) ;; *) exit 1 ;; esac && \\
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg iproute2 sqlite3 && \\
    curl -fsSL "https://downloads.osmocom.org/packages/osmocom:/$CHANNEL/xUbuntu_26.04/Release.key" | \\
      gpg --dearmor -o /usr/share/keyrings/osmocom.gpg && \\
    echo "deb [signed-by=/usr/share/keyrings/osmocom.gpg] https://downloads.osmocom.org/packages/osmocom:/$CHANNEL/xUbuntu_26.04/ /" > /etc/apt/sources.list.d/osmocom.list && \\
    printf 'Package: *\\nPin: origin downloads.osmocom.org\\nPin-Priority: 700\\n' > /etc/apt/preferences.d/osmocom && \\
    printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d && \\
    apt-get update && apt-get install -y --no-install-recommends \\
      osmo-stp osmo-hlr osmo-mgw osmo-msc osmo-bsc osmo-sgsn osmo-ggsn abisip-find osmo-bsc-ipaccess-utils && \\
    rm -rf /var/lib/apt/lists/* /usr/sbin/policy-rc.d
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
'''
ENTRYPOINT = '''#!/bin/sh
set -eu
umask 077
mkdir -p /var/lib/osmocom-lab/sgsn /var/lib/osmocom-lab/ggsn
exec "$@"
'''


def compose(s, no_new_privileges=True):
    deps = {'osmo-msc': ('osmo-stp', 'osmo-hlr', 'osmo-mgw'),
            'osmo-bsc': ('osmo-stp', 'osmo-msc', 'osmo-mgw'),
            'osmo-sgsn': ('osmo-hlr', 'osmo-ggsn')}
    text = 'name: osmocom-standalone\nservices:\n'
    for name in core.SERVICES:
        security = '    security_opt: [no-new-privileges:true]\n' if no_new_privileges else ''
        command = [f'/usr/bin/{name}', '-c', f'/etc/osmocom-lab/{name}.cfg']
        if name == 'osmo-hlr':
            command += ['-l', '/var/lib/osmocom-lab/hlr.db']
        text += f'''  {name}:
    image: osmocom-standalone:{s.channel}
    build:
      context: .
      args:
        CHANNEL: {s.channel}
    network_mode: host
    restart: unless-stopped
    init: true
    command: {json.dumps(command)}
    cap_drop: [ALL]
{security}    read_only: true
    tmpfs: [/tmp]
    volumes:
      - ./config:/etc/osmocom-lab:ro
      - state:/var/lib/osmocom-lab
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
'''
        if name == 'osmo-ggsn':
            text += '    cap_add: [NET_ADMIN]\n    devices: [/dev/net/tun:/dev/net/tun]\n'
        if name in deps:
            text += '    depends_on: ' + json.dumps(list(deps[name])) + '\n'
    return text + 'volumes:\n  state:\n'


def write_deployment(directory: Path, s, no_new_privileges=None):
    if no_new_privileges is None:
        no_new_privileges = not docker_is_snap()
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    config = directory / 'config'
    config.mkdir(exist_ok=True, mode=0o750)
    for name, body in core.render(s).items():
        path = config / f'{name}.cfg'
        path.write_text(body)
        path.chmod(0o640)
    (config / 'settings.json').write_text(json.dumps(dataclasses.asdict(s), indent=2) + '\n')
    (config / 'settings.json').chmod(0o640)
    for name, content in [('compose.yaml', compose(s, no_new_privileges)), ('Dockerfile', DOCKERFILE), ('entrypoint.sh', ENTRYPOINT)]:
        (directory / name).write_text(content)
        (directory / name).chmod(0o755 if name.endswith('.sh') else 0o644)
    for path in (SCRIPT, SCRIPT.with_name('install-osmocom-ubuntu2604.py')):
        destination = directory / path.name
        if path != destination:
            shutil.copyfile(path, destination)
        destination.chmod(0o755)


def docker(directory, *args):
    validate_directory(directory)
    return run('docker', 'compose', '--project-directory', directory, '-f', directory / 'compose.yaml', *args)


def load_settings(directory):
    core.CONFIG = directory / 'config'
    return core.Settings(**json.loads((core.CONFIG / 'settings.json').read_text())).validate()


def is_managed_deployment(directory):
    config = directory / 'config'
    compose_path = directory / 'compose.yaml'
    if (directory.is_symlink() or config.is_symlink()
            or compose_path.is_symlink()
            or not (config / 'settings.json').is_file()
            or not compose_path.is_file()):
        return False
    return compose_path.read_text().startswith('name: osmocom-standalone\n')


def migrate_snap_deployment(directory):
    """Copy a managed /opt install into Snap storage without deleting its source."""
    source = DEFAULT_DIRECTORY
    if not is_managed_deployment(source):
        raise ValueError(f'{directory} is unmanaged and no managed migration source was found at {source}')
    entries = list(directory.iterdir()) if directory.exists() else []
    unexpected = [path.name for path in entries
                  if path.name != '__pycache__' or path.is_symlink() or not path.is_dir()]
    if unexpected:
        raise ValueError(f'Snap destination contains unrelated files ({", ".join(unexpected)}); '
                         'no files were moved automatically')
    source_links = [path for path in (source, *source.rglob('*')) if path.is_symlink()]
    if source_links:
        raise ValueError(f'Migration source contains symlinks ({source_links[0]}); '
                         'review them before moving the deployment')
    shutil.copytree(source, directory, dirs_exist_ok=True)
    (directory / 'migration-source.txt').write_text(str(source) + '\n')
    print(f'Migrated managed deployment from {source} into {directory}; the source copy was retained.')


def boot_unit(directory):
    # Quoting is deliberately avoided: validated install paths contain no spaces or metacharacters.
    snap = docker_is_snap()
    engine_unit = 'snap.docker.dockerd.service' if snap else 'docker.service'
    # systemd's default PATH does not necessarily include the Snap CLI launcher.
    environment = ('Environment=PATH=/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
                   if snap else '')
    return f'''[Unit]
Description=Standalone Osmocom Docker lab and scoped firewall
Requires={engine_unit}
After={engine_unit} network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
{environment}ExecStart=/usr/bin/python3 {directory}/install-osmocom-docker.py start --directory {directory}
ExecStop=/usr/bin/python3 {directory}/install-osmocom-docker.py stop --directory {directory}
[Install]
WantedBy=multi-user.target
'''


def install(directory, s, replace, start):
    validate_directory(directory)
    snap_workaround = docker_is_snap()
    if snap_workaround:
        print('WARNING: Automatic Snap workaround: no-new-privileges will be omitted. '
              'Capability restrictions and read-only mounts/filesystems remain enabled.')
    migrated = False
    existing = (directory / 'config/settings.json').is_file()
    if (snap_workaround and directory == SNAP_DIRECTORY and replace and not existing
            and is_managed_deployment(DEFAULT_DIRECTORY)):
        migrate_snap_deployment(directory)
        migrated = True
        existing = True
    if directory.exists() and any(directory.iterdir()) and not replace:
        raise ValueError('Deployment directory exists; use --replace to back up configs and retain the named data volume')
    if directory.exists() and any(directory.iterdir()) and not existing:
        raise ValueError('Directory is not a managed Docker lab deployment; refusing to replace it')
    unit = BOOT_UNIT
    if unit.exists() and not (replace and existing):
        raise ValueError('Docker lab boot unit already exists; refusing to replace an unrelated installation')
    if core.CONFIG.exists():
        raise ValueError('Native Osmocom lab already installed; do not run native and Docker labs on the same ports')
    core.preflight(s, replace and existing, native=False)
    if shutil.which('docker'):
        run('docker', 'info')
        run('docker', 'compose', 'version')
    else:
        # Ubuntu packages, without changing docker-group membership or fetching remote shell scripts.
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', '--no-install-recommends', 'docker.io', 'docker-compose-v2')
        run('systemctl', 'enable', '--now', 'docker.service')
        run('docker', 'compose', 'version')
    endpoint = run('docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}', capture=True).stdout.strip()
    security = run('docker', 'info', '--format', '{{json .SecurityOptions}}', capture=True).stdout
    if not endpoint.startswith('unix://') or 'rootless' in security or 'userns' in security:
        raise ValueError('Use a local rootful Linux Docker engine without user-namespace remapping for this host-network lab')
    # Docker may just have created bridge routes; check the APN pool again.
    core.preflight(s, replace and existing, native=False)
    if directory.exists() and any(directory.iterdir()):
        backup = directory.with_name(directory.name + '.backup-' + core.datetime.datetime.now(core.datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        shutil.copytree(directory, backup)
        print('Deployment backup:', backup)
        defer_unit_stop = migrated and unit.exists()
        if unit.exists() and not defer_unit_stop:
            # Reset RemainAfterExit: a later start must execute the new helper.
            run('systemctl', 'stop', unit.name)
        docker(directory, 'down')  # Preserve subscribers/SMS during rebuild.
        core.firewall(load_settings(directory), False)
    run('apt-get', 'install', '-y', '--no-install-recommends', 'iptables', 'iproute2', 'sqlite3', 'tcpdump')
    run('modprobe', 'tun')
    write_deployment(directory, s, no_new_privileges=not snap_workaround)
    docker(directory, 'config', '--quiet')
    docker(directory, 'build')
    unit.write_text(boot_unit(directory))
    run('systemd-analyze', 'verify', unit)
    run('systemctl', 'daemon-reload')
    if existing and migrated and unit.exists():
        # The previous ExecStop may point to /opt, which Snap Docker cannot access.
        # After reloading this unit, its stop hook uses the new Snap-accessible helper.
        run('systemctl', 'stop', unit.name)
    run('systemctl', 'enable', unit.name)
    if start:
        run('systemctl', 'start', unit.name)
    print(f'\nDocker lab prepared. RF is {"LOCKED" if s.rf_locked else "ENABLED"} in the generated config; no BTS settings were written.')
    print('Deployment:', directory)
    print('Core started.' if start else 'Start: sudo systemctl start osmocom-docker-lab')
    print(f'Control: sudo python3 {directory}/install-osmocom-docker.py status --directory {directory}')


def uninstall(directory, keep_data=False):
    validate_directory(directory)
    if not is_managed_deployment(directory):
        raise ValueError('Directory is not a managed Docker lab deployment; refusing to uninstall')
    s = load_settings(directory)
    migration_source = directory / 'migration-source.txt'
    migrated_from = migration_source.read_text().strip() if migration_source.is_file() else None
    if migrated_from and migrated_from != str(DEFAULT_DIRECTORY):
        raise ValueError(f'Unknown migration source marker in {directory}; refusing to remove another path')
    if BACKUPS.is_symlink():
        raise ValueError(f'Backup path is a symlink; refusing to remove it: {BACKUPS}')
    if keep_data:
        core.backup_directory(directory, BACKUPS)
    if BOOT_UNIT.exists() or BOOT_UNIT.is_symlink():
        expected = f'ExecStart=/usr/bin/python3 {directory}/install-osmocom-docker.py start --directory {directory}'
        if BOOT_UNIT.is_symlink() or expected not in BOOT_UNIT.read_text().splitlines():
            raise ValueError('Docker lab boot unit belongs to another deployment; refusing to uninstall')
        run('systemctl', 'disable', '--now', BOOT_UNIT.name)
    # Remove this project's containers and volume by default. --keep-data preserves
    # the named subscriber/SMS volume while still removing the deployment itself.
    docker(directory, 'down', *([] if keep_data else ['--volumes']))
    core.firewall(s, False)
    BOOT_UNIT.unlink(missing_ok=True)
    shutil.rmtree(directory)
    if migrated_from and is_managed_deployment(DEFAULT_DIRECTORY):
        shutil.rmtree(DEFAULT_DIRECTORY)
    run('systemctl', 'daemon-reload')
    images_in_use = []
    for channel in ('nightly', 'latest'):
        image = f'osmocom-standalone:{channel}'
        present = run('docker', 'image', 'inspect', image, check=False, capture=True)
        if present.returncode:
            continue
        result = run('docker', 'image', 'rm', image, check=False, capture=True)
        if result.returncode:
            images_in_use.append(image)
    if not keep_data:
        # Remove dated replace backups created beside this deployment.
        backup_sources = {directory.parent: directory.name}
        if migrated_from:
            backup_sources[DEFAULT_DIRECTORY.parent] = DEFAULT_DIRECTORY.name
        for parent, deployment_name in backup_sources.items():
            backup_pattern = re.compile(re.escape(deployment_name) + r'\.backup-\d{8}T\d{12}Z\Z')
            for backup in parent.iterdir():
                if backup_pattern.fullmatch(backup.name) and not backup.is_symlink() and is_managed_deployment(backup):
                    shutil.rmtree(backup)
        shutil.rmtree(BACKUPS, ignore_errors=True)
    if keep_data:
        print('Docker lab uninstalled: containers and configs removed; the data volume was retained.')
        print('Subscriber/SMS data retained in Docker volume: osmocom-standalone_state')
    else:
        print('Docker lab uninstalled: containers, configs, data volume and lab backups removed.')
    if images_in_use:
        print('Images still used by other containers were retained:', ', '.join(images_in_use))
    print('Docker Engine and resources for other Compose projects remain installed.')
    print('Runtime IP forwarding was not changed.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('install', 'render', 'start', 'stop', 'status', 'rf-on', 'rf-off', 'logs', 'uninstall'):
        p = sub.add_parser(command, aliases={'rf-on': ['rf-enable'], 'rf-off': ['rf-disable']}.get(command, []))
        p.add_argument('--directory', type=Path,
                       help='Deployment path (default: installed helper directory, or '
                            '/var/snap/docker/common/osmocom-lab-docker for Snap Docker, '
                            '/opt/osmocom-lab-docker otherwise)')
        if command in ('install', 'render'):
            core.add_settings_arguments(p)
            p.add_argument('--allow-snap-security-workaround', action='store_true',
                           help='Compatibility option; the Snap workaround is now automatic. '
                                'Regular Docker retains no-new-privileges')
        if command == 'install':
            p.add_argument('--replace', action='store_true')
            p.add_argument('--start', action=argparse.BooleanOptionalAction, default=True,
                           help='Start the core after installation (default: enabled)')
        if command == 'uninstall':
            data = p.add_mutually_exclusive_group()
            data.add_argument('--keep-data', action='store_true',
                              help='Keep the named subscriber/SMS volume and back up configs before removal')
            data.add_argument('--purge-data', action='store_true', help=argparse.SUPPRESS)
        if command == 'rf-on':
            p.add_argument('--confirm', help='Legacy compatibility option; confirmation is no longer required')
    args = parser.parse_args(argv)
    args.command = {'rf-enable': 'rf-on', 'rf-disable': 'rf-off'}.get(args.command, args.command)
    directory = (args.directory or default_directory(args.command)).resolve()
    if args.command == 'render':
        if directory.exists() and any(directory.iterdir()):
            raise ValueError('Render directory must be empty')
        write_deployment(directory, core.settings_from_args(args))
        print('Docker deployment preview:', directory, '; no Docker/system/BTS changes')
        return
    if os.geteuid() != 0:
        raise ValueError('Use sudo for this command; render requires no root')
    validate_directory(directory)
    if args.command == 'install':
        install(directory, core.settings_from_args(args), args.replace, args.start)
        return
    if args.command == 'uninstall':
        uninstall(directory, args.keep_data)
        return
    s = load_settings(directory)
    if args.command == 'start':
        run('modprobe', 'tun')
        core.firewall(s, True)
        try:
            docker(directory, 'up', '-d', '--no-build')
        except subprocess.CalledProcessError:
            try:
                docker(directory, 'down')
            finally:
                core.firewall(s, False)
            raise
    elif args.command == 'stop':
        docker(directory, 'down')
        core.firewall(s, False)
    elif args.command == 'status':
        docker(directory, 'ps', '-a')
        run('ss', '-lntup')
    elif args.command == 'logs':
        docker(directory, 'logs', '--tail', '100', '--follow')
    else:
        core.change_rf(args.command == 'rf-off')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f'ERROR: {error}\nReview completed steps before retrying.')
