#!/usr/bin/env python3
"""Standalone native nanoBTS lab installer/controller. Python standard library only."""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import string

CONFIG = Path(os.environ.get('OSMOCOM_LAB_CONFIG', '/etc/osmocom-lab'))
STATE = Path('/var/lib/osmocom-lab')
HELPER = Path('/usr/local/sbin/osmocom-lab')
UNIT_DIRECTORY = Path('/etc/systemd/system')
BACKUPS = Path('/var/backups/osmocom-lab')
SYSCTL = Path('/etc/sysctl.d/90-osmocom-lab.conf')
APT_SOURCE = Path('/etc/apt/sources.list.d/osmocom-lab.list')
APT_PREFERENCES = Path('/etc/apt/preferences.d/osmocom-lab')
KEYRING_DIRECTORY = Path('/usr/share/keyrings')
SERVICES = ('osmo-stp', 'osmo-hlr', 'osmo-mgw', 'osmo-msc',
            'osmo-ggsn', 'osmo-sgsn', 'osmo-bsc')
PACKAGES = (*SERVICES, 'abisip-find', 'osmo-bsc-ipaccess-utils')
# Installed explicitly from the selected Osmocom feed: an older distro build
# with the same SONAME may already satisfy package dependencies while lacking
# symbols required by the current nightly daemons.
SHARED_RUNTIME_PACKAGES = ('libosmonetif11',)
WELCOME_LOG_RE = re.compile(r'VLR: update for IMSI=(\d+) \(MSISDN=(\d+)\)')
CHAINS = {'INPUT': 'OSMOLAB-IN', 'FORWARD': 'OSMOLAB-FWD', 'POSTROUTING': 'OSMOLAB-NAT'}
RFC1918 = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
BANDS = {'GSM850': ((128, 251),), 'GSM900': ((0, 124), (975, 1023)),
         'DCS1800': ((512, 885),), 'PCS1900': ((512, 810),)}


@dataclasses.dataclass
class Settings:
    lan_ip: str
    bts_ip: str
    lab_interface: str
    uplink: str
    arfcn: int
    band: str = 'DCS1800'
    power_reduction: int = 20
    name: str = 'GSMLab'
    mcc: str = '001'
    mnc: str = '01'
    unit: int = 1
    pool: str = '172.16.23.0/24'
    registration: str = 'open'
    unknown_apns: bool = True
    internet: bool = True
    channel: str = 'nightly'
    rf_locked: bool = False
    gprs_mode: str = 'egprs'
    welcome_sms: bool = False
    welcome_message: str = 'Your number is {msisdn}'
    welcome_sender: str = '100'

    def validate(self):
        for value in (self.lan_ip, self.bts_ip):
            addr = ipaddress.ip_address(value)
            if addr.version != 4 or not any(addr in n for n in RFC1918):
                raise ValueError('LAN and BTS addresses must be RFC1918 IPv4 addresses')
        if self.lan_ip == self.bts_ip:
            raise ValueError('Laptop and BTS addresses must differ')
        for value in (self.lab_interface, self.uplink):
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', value):
                raise ValueError('Invalid interface name')
        if self.band not in BANDS or not any(a <= self.arfcn <= b for a, b in BANDS[self.band]):
            raise ValueError('ARFCN does not belong to the selected GSM band')
        if self.power_reduction not in range(0, 25, 2):
            raise ValueError('Power reduction must be an even number from 0 to 24 dB')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}', self.name):
            raise ValueError('Network name must be 1–32 plain letters, digits, spaces, dots, underscores or hyphens')
        if not re.fullmatch(r'\d{3}', self.mcc) or not re.fullmatch(r'\d{2,3}', self.mnc):
            raise ValueError('MCC needs three digits; MNC needs two or three')
        if not 0 <= self.unit <= 65534:
            raise ValueError('IPA site/unit must be 0–65534')
        pool = ipaddress.ip_network(self.pool)
        if pool.version != 4 or not 16 <= pool.prefixlen <= 29 or not any(pool.subnet_of(n) for n in RFC1918):
            raise ValueError('APN pool must be an RFC1918 IPv4 network, /16 through /29')
        if ipaddress.ip_address(self.lan_ip) in pool or ipaddress.ip_address(self.bts_ip) in pool:
            raise ValueError('APN pool must not contain the laptop or BTS address')
        if self.registration not in ('closed', 'open', 'observe') or self.channel not in ('nightly', 'latest'):
            raise ValueError('Invalid registration policy or package channel')
        if self.gprs_mode not in ('gprs', 'egprs'):
            raise ValueError('Packet mode must be gprs or egprs')
        if not isinstance(self.rf_locked, bool):
            raise ValueError('RF lock must be a boolean')
        if not isinstance(self.welcome_sms, bool):
            raise ValueError('Welcome SMS setting must be a boolean')
        if not re.fullmatch(r'\d{1,15}', self.welcome_sender):
            raise ValueError('Welcome SMS sender must contain 1–15 digits')
        if (not self.welcome_message or len(self.welcome_message) > 140
                or any(ord(char) < 32 for char in self.welcome_message)):
            raise ValueError('Welcome SMS must be 1–140 characters without control characters')
        try:
            fields = [field for _, field, _, _ in string.Formatter().parse(self.welcome_message) if field]
            self.welcome_message.format(msisdn='132')
        except (ValueError, KeyError, IndexError) as error:
            raise ValueError('Welcome SMS template supports only the {msisdn} field') from error
        if any(field != 'msisdn' for field in fields):
            raise ValueError('Welcome SMS template supports only the {msisdn} field')
        return self


def run(*args, check=True, capture=False):
    print('+', ' '.join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=check, text=True,
                          env={**os.environ, 'LC_ALL': 'C'},
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def _msc_vty(command: str) -> str:
    if not re.fullmatch(r'[^\r\n\x00-\x1f]{1,320}', command):
        raise ValueError('Invalid MSC VTY command')
    with socket.create_connection(('127.0.0.1', 4254), timeout=5) as connection:
        connection.settimeout(2)
        try:
            connection.recv(8192)
        except socket.timeout:
            pass
        connection.sendall(('enable\n' + command + '\n').encode('utf-8'))
        time.sleep(0.2)
        try:
            response = connection.recv(16384).decode('utf-8', errors='replace')
        except socket.timeout as error:
            raise RuntimeError('No response from the OsmoMSC VTY') from error
    if not response or '%' in response or 'failed' in response.lower():
        raise RuntimeError(response.strip() or 'Empty response from the OsmoMSC VTY')
    return response


def _welcome_sent_file() -> Path:
    return STATE / 'welcome-sms-sent.json'


def _load_welcome_sent() -> dict[str, str]:
    path = _welcome_sent_file()
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise ValueError(f'Invalid welcome SMS delivery state: {path}')
    return data


def _record_welcome_sent(sent: dict[str, str], imsi: str) -> None:
    sent[imsi] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = _welcome_sent_file()
    with tempfile.NamedTemporaryFile(mode='w', dir=STATE, prefix='welcome-sms-',
                                     suffix='.tmp', delete=False) as temporary:
        temporary.write(json.dumps(sent, indent=2) + '\n')
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, path)


def _send_welcome_sms(settings: Settings, sent: dict[str, str], imsi: str, msisdn: str) -> None:
    if imsi in sent or not settings.welcome_sms:
        return
    message = settings.welcome_message.format(msisdn=msisdn)
    command = (f'subscriber msisdn {msisdn} sms sender msisdn '
               f'{settings.welcome_sender} send {message}')
    for attempt in range(6):
        try:
            _msc_vty(command)
            _record_welcome_sent(sent, imsi)
            print('Welcome SMS queued for a newly registered subscriber.', flush=True)
            return
        except (OSError, RuntimeError) as error:
            if attempt == 5:
                print(f'Welcome SMS could not be submitted; it will retry on the next registration: {error}',
                      file=sys.stderr, flush=True)
                return
            time.sleep(2)


def welcome_monitor() -> None:
    settings_path = CONFIG / 'settings.json'
    settings = Settings(**json.loads(settings_path.read_text())).validate()
    if not settings.welcome_sms:
        print('Welcome SMS is disabled in the saved settings.', flush=True)
        return
    sent = _load_welcome_sent()
    journalctl = '/usr/bin/journalctl'
    command = [journalctl, '--follow', '--lines=0', '--output=cat',
               '--unit=osmocom-lab-osmo-msc.service']
    while True:
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True,
                              encoding='utf-8', errors='replace', bufsize=1) as journal:
            assert journal.stdout is not None
            for line in journal.stdout:
                match = WELCOME_LOG_RE.search(line)
                if match:
                    _send_welcome_sms(settings, sent, match.group(1), match.group(2))
            result = journal.wait()
        print(f'journalctl exited with status {result}; retrying in five seconds.',
              file=sys.stderr, flush=True)
        time.sleep(5)


def render(s: Settings) -> dict[str, str]:
    s.validate()
    common = '''log stderr
 logging filter all 1
 logging color 0
 logging timestamp 1
 logging print category 1
 logging print level 1
 logging level set-all notice
!
line vty
 bind 127.0.0.1
 no login
!
ctrl
 bind 127.0.0.1
!
'''
    auto = '' if s.registration == 'closed' else f' subscriber-create-on-demand 3 {"none" if s.registration == "observe" else "cs+ps"}\n'
    wildcard = '   profile 2\n    apn *\n' if s.unknown_apns else ''
    files = {}
    files['osmo-stp'] = common.replace('ctrl\n bind 127.0.0.1\n!\n', '') + '''cs7 instance 0
 xua rkm routing-key-allocation dynamic-permitted
 listen m3ua 2905
  local-ip 127.0.0.1
  accept-asp-connections dynamic-permitted
'''
    files['osmo-hlr'] = common + f'''hlr
{auto} store-imei
 gsup
  bind ip 127.0.0.1
 ussd route prefix *#100# internal own-msisdn
 ussd route prefix *#101# internal own-imsi
 ps
  pdp-profiles default
   profile 1
    apn internet
{wildcard}'''
    files['osmo-mgw'] = common + f'''mgcp
 bind ip 127.0.0.1
 bind port 2427
 rtp bind-ip {s.lan_ip}
 rtp port-range 4002 16001
 rtp ip-probing
 number endpoints 512
 rtcp-omit
'''
    files['osmo-msc'] = common + f'''network
 network country code {s.mcc}
 mobile network code {s.mnc}
 short name {s.name}
 long name {s.name}
 encryption a5 0
 rrlp mode none
 mm info 1
 mgw 0
  remote-ip 127.0.0.1
  remote-port 2427
  local-port 2728
mncc-int
 default-codec tch-f fr
msc
 assign-tmsi
 check-imei-rqd early
 auth-tuple-max-reuse-count 3
 auth-tuple-reuse-on-error 1
 mncc internal
smsc
 database {STATE}/sms.db
 database delete-delivered 0
 database delete-expired 0
 queue max-failure 3
'''
    slots = ('CCCH+SDCCH4', 'SDCCH8', 'TCH/F', 'TCH/F', 'TCH/F_PDCH', 'TCH/F_PDCH', 'TCH/F_PDCH', 'PDCH')
    files['osmo-bsc'] = common + f'''e1_input
 e1_line 0 driver ipa
network
 network country code {s.mcc}
 mobile network code {s.mnc}
 short name {s.name}
 long name {s.name}
 encryption a5 0
 neci 1
 paging any use tch 0
 handover 0
 timer net T3212 5
 mgw 0
  remote-ip 127.0.0.1
  remote-port 2427
  local-port 2727
 bts 0
  type nanobts
  band {s.band}
  cell_identity 24
  location_area_code 23
  base_station_id_code 24
  ms max power 15
  cell reselection hysteresis 4
  rxlev access min 0
  radio-link-timeout 32
  channel allocator mode set-all ascending
  rach tx integer 9
  rach max transmission 7
  channel-description attach 1
  channel-description bs-pa-mfrms 5
  channel-description bs-ag-blks-res 1
  early-classmark-sending forbidden
  ipa unit-id {s.unit} 0
  oml ipa stream-id 255 line 0
  codec-support fr
  gprs mode {s.gprs_mode}
  gprs routing area 1
  gprs network-control-order nc1
  gprs cell bvci 24
  gprs nsei 24
  gprs nsvc 0 nsvci 24
  gprs nsvc 0 local udp port 23000
  gprs nsvc 0 remote udp port 23000
  gprs nsvc 0 remote ip {s.lan_ip}
  trx 0
   rf_locked {int(s.rf_locked)}
   arfcn {s.arfcn}
   nominal power 23
   max_power_red {s.power_reduction}
   rsl e1 tei 0
''' + ''.join(f'   timeslot {i}\n    phys_chan_config {slot}\n' for i, slot in enumerate(slots)) + '''msc 0
 allow-emergency deny
bsc
 mid-call-timeout 0
'''
    files['osmo-sgsn'] = common + f'''sgsn
 gtp state-dir {STATE}/sgsn
 gtp local-ip 127.0.0.1
 ggsn 0 remote-ip 127.0.0.2
 ggsn 0 gtp-version 1
 ggsn 0 echo-interval 60
 authentication optional
 auth-policy remote
 gsup remote-ip 127.0.0.1
 gsup remote-port 4222
ns
 bind udp local
  listen {s.lan_ip} 23000
  accept-ipaccess
bssgp
'''
    files['osmo-ggsn'] = common + f'''stats interval 5
ggsn ggsn0
 gtp state-dir {STATE}/ggsn
 gtp bind-ip 127.0.0.2
 apn internet
  gtpu-mode tun
  tun-device tun-osmo
  type-support v4
  mtu 1420 apply
  ip prefix dynamic {s.pool}
  ip dns 0 1.1.1.1
  ip dns 1 8.8.8.8
  ip ifconfig {s.pool}
  no shutdown
 default-apn internet
 no shutdown ggsn
'''
    return files


def units(welcome_sms: bool = False) -> dict[str, str]:
    dependencies = {'osmo-msc': ('osmo-stp', 'osmo-hlr', 'osmo-mgw'),
                    'osmo-bsc': ('osmo-stp', 'osmo-msc', 'osmo-mgw'),
                    'osmo-sgsn': ('osmo-hlr', 'osmo-ggsn')}
    names = [f'osmocom-lab-{n}.service' for n in SERVICES]
    wanted = (*names, *(('osmocom-lab-welcome.service',) if welcome_sms else ()))
    result = {'osmocom-lab.target': '[Unit]\nDescription=Standalone Osmocom nanoBTS lab\n'
              'Wants=network-online.target ' + ' '.join(wanted) + '\nAfter=network-online.target\n'
              '[Install]\nWantedBy=multi-user.target\n'}
    result['osmocom-lab-firewall.service'] = f'''[Unit]
Description=Scoped Osmocom lab firewall rules
PartOf=osmocom-lab.target
Before={' '.join(names)}
After=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={HELPER} firewall-up
ExecStop={HELPER} firewall-down
'''
    for name in SERVICES:
        deps = ' '.join(f'osmocom-lab-{n}.service' for n in dependencies.get(name, ()))
        args = f'-c {CONFIG}/{name}.cfg'
        if name == 'osmo-hlr':
            args += f' -l {STATE}/hlr.db'
        caps = 'CAP_NET_ADMIN' if name == 'osmo-ggsn' else ''
        result[f'osmocom-lab-{name}.service'] = f'''[Unit]
Description=Standalone {name}
PartOf=osmocom-lab.target
Requires=osmocom-lab-firewall.service {deps}
After=network-online.target osmocom-lab-firewall.service {deps}
[Service]
Type=simple
User=osmocom-lab
Group=osmocom-lab
ExecStart=/usr/bin/{name} {args}
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={STATE}
CapabilityBoundingSet={caps}
AmbientCapabilities={caps}
StandardOutput=journal
StandardError=journal
'''
    result['osmocom-lab-welcome.service'] = f'''[Unit]
Description=Standalone Osmocom welcome SMS monitor
PartOf=osmocom-lab.target
Requires=osmocom-lab-osmo-msc.service
After=osmocom-lab-osmo-msc.service
[Service]
Type=simple
User=osmocom-lab
Group=osmocom-lab
SupplementaryGroups=systemd-journal
ExecStart={HELPER} welcome-monitor
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={STATE}
StandardOutput=journal
StandardError=journal
'''
    return result


def artifacts(s: Settings) -> dict[str, str]:
    output = {f'etc/osmocom-lab/{name}.cfg': body for name, body in render(s).items()}
    output.update({f'etc/systemd/system/{name}': body for name, body in units(s.welcome_sms).items()})
    output['etc/osmocom-lab/settings.json'] = json.dumps(dataclasses.asdict(s), indent=2) + '\n'
    if s.internet:
        output['etc/sysctl.d/90-osmocom-lab.conf'] = '# Owned by standalone Osmocom lab\nnet.ipv4.ip_forward=1\n'
    return output


def write_files(root: Path, s: Settings):
    for relative, content in artifacts(s).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temp_path = Path(temporary.name)
        temp_path.chmod(0o640 if relative.startswith('etc/osmocom-lab/') else 0o644)
        temp_path.replace(path)
    helper = root / str(HELPER).lstrip('/')
    helper.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != helper.resolve():
        shutil.copyfile(Path(__file__).resolve(), helper)
    helper.chmod(0o755)


def firewall_rules(s: Settings):
    rules = [('filter', 'OSMOLAB-IN', ['-i', s.lab_interface, '-s', s.bts_ip, '-d', s.lan_ip,
                                    '-p', 'tcp', '--dport', '3002:3003', '-j', 'ACCEPT'])]
    for ports in ('23000', '4002:16001'):
        rules.append(('filter', 'OSMOLAB-IN', ['-i', s.lab_interface, '-s', s.bts_ip, '-d', s.lan_ip,
                                             '-p', 'udp', '--dport', ports, '-j', 'ACCEPT']))
    rules.append(('filter', 'OSMOLAB-IN', ['-i', 'tun-osmo', '-s', s.pool, '-j', 'DROP']))
    if s.internet:
        rules.extend([
            ('filter', 'OSMOLAB-FWD', ['-i', 'tun-osmo', '-o', s.uplink, '-s', s.pool,
                                     '-m', 'conntrack', '--ctstate', 'NEW,ESTABLISHED,RELATED', '-j', 'ACCEPT']),
            ('filter', 'OSMOLAB-FWD', ['-i', s.uplink, '-o', 'tun-osmo', '-d', s.pool,
                                     '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT']),
            ('nat', 'OSMOLAB-NAT', ['-s', s.pool, '-o', s.uplink, '-j', 'MASQUERADE']),
        ])
    # No implicit internet access or unsolicited forwarding, even on an
    # otherwise permissive host. Only preceding scoped allowances can pass.
    rules.extend([('filter', 'OSMOLAB-FWD', ['-s', s.pool, '-j', 'DROP']),
                  ('filter', 'OSMOLAB-FWD', ['-d', s.pool, '-j', 'DROP'])])
    return rules


def firewall(s: Settings, up: bool):
    for parent, chain in CHAINS.items():
        table = 'nat' if parent == 'POSTROUTING' else 'filter'
        base = ('iptables', '-w', '5', '-t', table)
        hook = ('-m', 'comment', '--comment', 'osmocom-lab', '-j', chain)
        if up:
            if run(*base, '-S', chain, check=False, capture=True).returncode:
                run(*base, '-N', chain)
            run(*base, '-F', chain)  # Only this installer's owned chain.
            for rule_table, rule_chain, args in firewall_rules(s):
                if (rule_table, rule_chain) == (table, chain):
                    run(*base, '-A', chain, *args)
            if run(*base, '-C', parent, *hook, check=False, capture=True).returncode:
                run(*base, '-I', parent, '1', *hook)
        else:
            while not run(*base, '-C', parent, *hook, check=False, capture=True).returncode:
                run(*base, '-D', parent, *hook)
            if not run(*base, '-S', chain, check=False, capture=True).returncode:
                run(*base, '-F', chain)
                run(*base, '-X', chain)
    if up and s.internet:
        run('sysctl', '-w', 'net.ipv4.ip_forward=1')


def vty_rf(locked: bool):
    with socket.create_connection(('127.0.0.1', 4242), timeout=3) as sock:
        sock.settimeout(.3)
        def receive():
            data = b''
            while True:
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                except socket.timeout:
                    break
            return data.decode(errors='replace')
        receive()
        for cmd in ('enable', 'configure terminal', 'network', 'bts 0', 'trx 0',
                    f'rf_locked {int(locked)}', 'end', 'show trx 0 0'):
            sock.sendall((cmd + '\r\n').encode())
            out = receive()
            if re.search(r'^% ', out, re.M):
                raise RuntimeError(out)
            print(out)


def change_rf(locked: bool):
    path = CONFIG / 'osmo-bsc.cfg'
    content, count = re.subn(r'^   rf_locked [01]$', f'   rf_locked {int(locked)}', path.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Expected exactly one managed radio carrier; refusing to edit this config')
    # Locking must persist even if the controller is temporarily unreachable.
    # Failed activation, in contrast, must never persist an unlocked config.
    if locked:
        path.write_text(content)
    vty_rf(locked)
    if not locked:
        path.write_text(content)
    print('Saved RF lock:', locked, '(VTY state is not an RF measurement)')


def preflight(s: Settings, replace: bool, native: bool = True):
    os_release = dict(line.strip().split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if os_release.get('ID', '').strip('"') != 'ubuntu' or os_release.get('VERSION_ID', '').strip('"') != '26.04':
        raise ValueError('This installer supports Ubuntu 26.04 only; no cross-distribution package fallback')
    arch = run('dpkg', '--print-architecture', capture=True).stdout.strip()
    if arch not in ('amd64', 'arm64'):
        raise ValueError('Official Ubuntu 26.04 Osmocom feed supports amd64 and arm64')
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('A running systemd installation is required')
    if native and CONFIG.exists() and not replace:
        raise ValueError(f'{CONFIG} already exists; use --replace to back up and replace managed configs')
    if native and CONFIG.exists() and not (CONFIG / 'settings.json').is_file():
        raise ValueError('Existing config directory is not a managed standalone installation; refusing to replace it')
    if native and Path('/etc/systemd/system/osmocom-docker-lab.service').exists():
        raise ValueError('Docker Osmocom lab already installed; native and Docker variants must not share a host')
    if native and HELPER.exists() and not CONFIG.exists():
        raise ValueError('Existing osmocom-lab helper is not associated with a managed installation')
    if native and Path('/usr/sbin/policy-rc.d').exists():
        raise ValueError('Existing /usr/sbin/policy-rc.d: refusing to replace another package-start policy')
    for name in SERVICES:
        if not run('systemctl', 'is-active', '--quiet', name + '.service', check=False).returncode:
            raise ValueError(f'Existing vendor {name} service is running; use a clean laptop or resolve the conflict first')
    if not replace:
        shared_ports = {2123, 2152, 2427, 2905, 3002, 3003, 4222, 4239, 4242, 4243, 4245, 4254, 4258, 4260, 23000}
        listeners = run('ss', '-H', '-lntu', capture=True).stdout
        for line in listeners.splitlines():
            fields = line.split()
            if len(fields) > 4 and fields[4].rsplit(':', 1)[-1].isdigit():
                port = int(fields[4].rsplit(':', 1)[-1])
                if port in shared_ports:
                    raise ValueError(f'Required Osmocom port {port} already has a listener; resolve the conflict first')
    if shutil.which('ufw'):
        status = run('ufw', 'status', capture=True).stdout
        if re.search(r'^Status: active$', status, re.M):
            raise ValueError('UFW is active. Integrate/review lab firewall rules separately; installer will not disable it')
    if shutil.which('nft'):
        result = run('nft', '-j', 'list', 'ruleset', capture=True)
        for item in json.loads(result.stdout).get('nftables', []):
            chain = item.get('chain', {})
            if chain.get('family') in ('inet', 'netdev', 'bridge') and chain.get('hook') in ('input', 'forward', 'ingress'):
                raise ValueError('Custom nftables base chains found; review firewall integration before using this installer')
    addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show', 'dev', s.lab_interface, capture=True).stdout)
    if not any(a.get('local') == s.lan_ip for interface in addresses for a in interface.get('addr_info', [])):
        raise ValueError('LAN IP is not currently assigned to the specified lab interface; reserve/configure it first')
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all', capture=True).stdout)
    for route in routes:
        destination = route.get('dst', 'default')
        if destination != 'default' and ipaddress.ip_network(destination, strict=False).overlaps(ipaddress.ip_network(s.pool)):
            # A replacement installation legitimately owns its existing TUN route.
            if not (replace and route.get('dev') == 'tun-osmo'):
                raise ValueError(f'APN pool overlaps existing route {destination}; choose another --pool')
    if s.internet and not Path('/sys/class/net', s.uplink).exists():
        raise ValueError('Internet uplink interface does not exist')
    bts_route = json.loads(run('ip', '-j', '-4', 'route', 'get', s.bts_ip, capture=True).stdout)
    if not bts_route or bts_route[0].get('dev') != s.lab_interface:
        raise ValueError('BTS route does not use the chosen lab interface')
    if native and CONFIG.exists() and (CONFIG / 'settings.json').is_file():
        old = Settings(**json.loads((CONFIG / 'settings.json').read_text()))
        if old.internet and not s.internet:
            raise ValueError('Replace cannot silently remove existing internet sysctl policy; review that change manually')


def install(s: Settings, replace: bool, start: bool):
    preflight(s, replace and CONFIG.exists())
    # Repository is checked before any package or service mutation.
    repo = f'https://downloads.osmocom.org/packages/osmocom:/{s.channel}/xUbuntu_26.04/'
    with urllib.request.urlopen(repo + 'Release', timeout=20) as response:
        if 'xUbuntu_26.04' not in response.read().decode():
            raise ValueError('Unexpected repository Release file')
    if CONFIG.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = Path('/var/backups/osmocom-lab') / stamp
        backup.mkdir(parents=True, mode=0o700)
        shutil.copytree(CONFIG, backup / 'config')
        shutil.copytree('/etc/systemd/system', backup / 'units',
                        ignore=lambda path, names: [n for n in names if not n.startswith('osmocom-lab')])
        run('systemctl', 'stop', 'osmocom-lab.target')
        print('Config/unit backup:', backup, '; subscriber and SMS databases remain untouched')
    policy = Path('/usr/sbin/policy-rc.d')
    for name in SERVICES:
        run('systemctl', 'mask', name + '.service')
    policy.write_text('#!/bin/sh\n# Temporary standalone Osmocom installer guard\nexit 101\n')
    policy.chmod(0o755)
    try:
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', '--no-install-recommends',
            'ca-certificates', 'curl', 'gnupg', 'iptables', 'iproute2', 'sqlite3', 'tcpdump', 'netcat-openbsd')
        key = Path(f'/usr/share/keyrings/osmocom-lab-{s.channel}.gpg')
        with tempfile.TemporaryDirectory() as directory:
            ascii_key = Path(directory) / 'Release.key'
            run('curl', '-fsSL', '--proto', '=https', '--tlsv1.2', repo + 'Release.key', '-o', ascii_key)
            run('gpg', '--batch', '--yes', '--dearmor', '--output', key, ascii_key)
        key.chmod(0o644)
        Path('/etc/apt/sources.list.d/osmocom-lab.list').write_text(f'deb [signed-by={key}] {repo} /\n')
        Path('/etc/apt/preferences.d/osmocom-lab').write_text('Package: *\nPin: origin downloads.osmocom.org\nPin-Priority: 700\n')
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', '--no-install-recommends',
            *SHARED_RUNTIME_PACKAGES, *PACKAGES)
    finally:
        policy.unlink(missing_ok=True)
    for name in SERVICES:
        run('systemctl', 'disable', '--now', name + '.service')
        run('systemctl', 'mask', name + '.service')
    if run('id', '-u', 'osmocom-lab', check=False, capture=True).returncode:
        run('useradd', '--system', '--user-group', '--home-dir', STATE, '--shell', '/usr/sbin/nologin', 'osmocom-lab')
    for directory in (STATE, STATE / 'sgsn', STATE / 'ggsn'):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        run('chown', 'osmocom-lab:osmocom-lab', directory)
    run('modprobe', 'tun')
    if not Path('/dev/net/tun').exists():
        raise ValueError('/dev/net/tun unavailable; install did not start services')
    write_files(Path('/'), s)
    run('chown', '-R', 'root:osmocom-lab', CONFIG)
    CONFIG.chmod(0o750)
    run('systemd-analyze', 'verify', *('/etc/systemd/system/' + name for name in units(s.welcome_sms)))
    if s.internet:
        run('sysctl', '--load=/etc/sysctl.d/90-osmocom-lab.conf')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'osmocom-lab.target')
    versions = run('dpkg-query', '-W', '-f=${Package}\t${Version}\n',
                   *SHARED_RUNTIME_PACKAGES, *PACKAGES, capture=True).stdout
    (CONFIG / 'package-versions.txt').write_text(versions)
    if start:
        run('systemctl', 'start', 'osmocom-lab.target')
    print(f'\nInstalled. RF is {"LOCKED" if s.rf_locked else "ENABLED"} in the generated config. No BTS settings were written.')
    print('Configs:', CONFIG, '\nState:', STATE)
    print('Core started.' if start else 'Start with: sudo osmocom-lab start')
    print('Inspect with: sudo osmocom-lab status')


def settings_from_args(args) -> Settings:
    values = {}
    prompts = {'lan_ip': 'Laptop LAN IPv4 address', 'bts_ip': 'nanoBTS IPv4 address',
               'lab_interface': 'Ethernet/lab interface', 'uplink': 'Internet uplink interface (may be the same)',
               'arfcn': 'Licensed ARFCN'}
    for field in dataclasses.fields(Settings):
        value = getattr(args, field.name, field.default)
        if value is None and field.default is not dataclasses.MISSING:
            value = field.default
        if value is None:
            if not sys.stdin.isatty():
                raise ValueError(f'--{field.name.replace("_", "-")} is required in non-interactive mode')
            value = input(prompts[field.name] + ': ').strip()
            if field.name == 'arfcn':
                value = int(value)
        values[field.name] = value
    return Settings(**values).validate()


def backup_directory(source: Path, parent: Path) -> Path:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = parent / stamp
    destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    shutil.copytree(source, destination / source.name, symlinks=True)
    print('Configuration backup:', destination)
    return destination


def uninstall(purge_data=False, keep_packages=False):
    if CONFIG.is_symlink() or not (CONFIG / 'settings.json').is_file():
        raise ValueError('Config directory is not a managed native lab installation; refusing to uninstall')
    s = Settings(**json.loads((CONFIG / 'settings.json').read_text())).validate()
    if purge_data and STATE.is_symlink():
        raise ValueError('State directory is a symlink; refusing to purge it')
    managed_units = []
    for name, body in units(s.welcome_sms).items():
        path = UNIT_DIRECTORY / name
        if path.exists() or path.is_symlink():
            description = next(line for line in body.splitlines() if line.startswith('Description='))
            if path.is_symlink() or description not in path.read_text().splitlines():
                raise ValueError(f'Unit is not owned by the native lab: {path}')
            managed_units.append(path)
    # Stop individual services too: they may have been started without the target.
    if managed_units:
        run('systemctl', 'stop', *(path.name for path in managed_units))
    if any(path.name == 'osmocom-lab.target' for path in managed_units):
        run('systemctl', 'disable', 'osmocom-lab.target')
    firewall(s, False)
    if not purge_data:
        backup_directory(CONFIG, BACKUPS)
    if not keep_packages:
        # No autoremove/purge: shared utilities and vendor configs remain untouched.
        run('apt-get', 'remove', '-y', *PACKAGES)
        run('systemctl', 'unmask', *(name + '.service' for name in SERVICES))
    for path in managed_units:
        path.unlink()
    for path in (HELPER, SYSCTL, APT_SOURCE, APT_PREFERENCES,
                 *(KEYRING_DIRECTORY / f'osmocom-lab-{channel}.gpg' for channel in ('nightly', 'latest'))):
        path.unlink(missing_ok=True)
    shutil.rmtree(CONFIG)
    if purge_data and STATE.exists():
        shutil.rmtree(STATE)
    run('systemctl', 'daemon-reload')
    print('Native lab uninstalled.')
    if not purge_data:
        print('Subscriber/SMS state retained:', STATE)
    if keep_packages:
        print('Osmocom packages retained; vendor services remain masked.')
    print('Existing backups, the service account and shared host utilities were retained. '
          'Runtime IP forwarding was not changed.')


def add_settings_arguments(p, welcome_sms=False):
    for name in ('lan-ip', 'bts-ip', 'lab-interface', 'uplink'):
        p.add_argument('--' + name)
    p.add_argument('--arfcn', type=int)
    for name, default in (('band', 'DCS1800'), ('name', 'GSMLab'), ('mcc', '001'), ('mnc', '01'), ('pool', '172.16.23.0/24')):
        p.add_argument('--' + name, default=default)
    p.add_argument('--power-reduction', type=int, default=20, help='TRX power reduction in dB (default: 20)')
    p.add_argument('--unit', type=int, default=1)
    p.add_argument('--registration', choices=('closed', 'open', 'observe'), default='open',
                   help='Subscriber registration policy (default: open)')
    p.add_argument('--unknown-apns', action=argparse.BooleanOptionalAction, default=True,
                   help='Allow unknown APNs to use the internet gateway (default: enabled)')
    p.add_argument('--internet', action=argparse.BooleanOptionalAction, default=True,
                   help='Enable scoped outbound internet forwarding (default: enabled)')
    p.add_argument('--gprs-mode', choices=('gprs', 'egprs'), default='egprs',
                   help='Packet radio mode (default: egprs / EDGE)')
    if welcome_sms:
        p.add_argument('--welcome-sms', action=argparse.BooleanOptionalAction, default=False,
                       help='Send one welcome SMS after a subscriber first registers (default: disabled)')
        p.add_argument('--welcome-message', default='Your number is {msisdn}',
                       help='Welcome SMS template; supports {msisdn}')
        p.add_argument('--welcome-sender', default='100',
                       help='Welcome SMS sender MSISDN (default: 100)')
    rf = p.add_mutually_exclusive_group()
    rf.add_argument('--rf-enable', dest='rf_locked', action='store_false',
                    help='Generate an RF-enabled config (default)')
    rf.add_argument('--rf-disable', dest='rf_locked', action='store_true',
                    help='Generate an RF-locked config')
    p.set_defaults(rf_locked=False)
    p.add_argument('--channel', choices=('nightly', 'latest'), default='nightly')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('install', 'render'):
        p = sub.add_parser(command, help='Install natively' if command == 'install' else 'Write a preview only; no root/network/system changes')
        add_settings_arguments(p, welcome_sms=True)
        if command == 'render':
            p.add_argument('--output', type=Path, required=True)
        else:
            p.add_argument('--replace', action='store_true', help='Back up/replace owned configs; retain databases; apply selected RF state')
            p.add_argument('--start', action=argparse.BooleanOptionalAction, default=True,
                           help='Start the core after installation (default: enabled)')
    for command in ('start', 'stop', 'status', 'rf-on', 'rf-off', 'firewall-up', 'firewall-down'):
        p = sub.add_parser(command, aliases={'rf-on': ['rf-enable'], 'rf-off': ['rf-disable']}.get(command, []))
        if command == 'rf-on':
            p.add_argument('--confirm', help='Legacy compatibility option; confirmation is no longer required')
    sub.add_parser('welcome-monitor', help=argparse.SUPPRESS)
    p = sub.add_parser('uninstall', help='Remove the native lab; back up configs and retain databases by default')
    p.add_argument('--purge-data', action='store_true', help='Delete lab configs and subscriber/SMS databases without a new backup')
    p.add_argument('--keep-packages', action='store_true', help='Retain Osmocom packages with vendor services still masked')
    args = parser.parse_args(argv)
    args.command = {'rf-enable': 'rf-on', 'rf-disable': 'rf-off'}.get(args.command, args.command)
    if args.command == 'render':
        if args.output.exists() and any(args.output.iterdir()):
            raise ValueError('Preview directory must be empty; existing files will not be overwritten')
        write_files(args.output, settings_from_args(args))
        print('Preview written:', args.output, '; no system or BTS changes')
        return
    if args.command == 'welcome-monitor':
        welcome_monitor()
        return
    if os.geteuid() != 0:
        raise ValueError('This command requires root (sudo); render does not')
    if args.command == 'install':
        install(settings_from_args(args), args.replace, args.start)
    elif args.command == 'uninstall':
        uninstall(args.purge_data, args.keep_packages)
    elif args.command in ('firewall-up', 'firewall-down'):
        firewall(Settings(**json.loads((CONFIG / 'settings.json').read_text())).validate(), args.command == 'firewall-up')
    elif args.command in ('rf-on', 'rf-off'):
        change_rf(args.command == 'rf-off')
    elif args.command == 'status':
        run('systemctl', '--no-pager', '--full', 'status', 'osmocom-lab.target',
            *(f'osmocom-lab-{name}.service' for name in SERVICES),
            'osmocom-lab-welcome.service', check=False)
        run('ss', '-lntup')
    else:
        run('systemctl', args.command, 'osmocom-lab.target')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f'ERROR: {error}\nReview completed steps before retrying.')
