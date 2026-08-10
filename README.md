# config-integrity

`config-integrity` is a small Python 3 tool for Debian 13 that records an
explicitly trusted SHA-256 baseline of locally modified Debian configuration
files. It uses `debsums -e` as its Debian-file input and can also monitor a
short, explicit list of user-managed files.

It detects change relative to a **local baseline**. It does not prove that a
machine or the baseline was uncompromised when the baseline was created.

## Requirements and installation

- Debian 13
- Python 3 (standard library only)
- `debsums`
- root privileges for operational commands

Install the prerequisite and copy the executable to a root-owned location:

```sh
sudo apt install debsums
sudo install -o root -g root -m 0755 config-integrity /usr/local/sbin/config-integrity
config-integrity --help
```

No Python package or pip dependency is required.

To evaluate it directly from a checkout without installing the script:

```sh
chmod 0755 config-integrity
sudo ./config-integrity init
sudo ./config-integrity check
```

Do not run `init --force` or accept `update` until the displayed files are
known-good local configuration. Those commands establish trust; they do not
independently determine whether the current contents are safe.

## Commands

Create the first baseline:

```sh
sudo config-integrity init
```

`init` refuses to replace an existing baseline unless `--force` is supplied.
It prints every file being trusted. The default baseline is
`/var/lib/config-integrity/baseline.json`; its directory is mode `0700` and
the atomically written JSON file is mode `0600`.

Check without changing trust state:

```sh
sudo config-integrity check
sudo config-integrity check --verbose
```

Normal output shows anomalies only. `--verbose` includes `UNCHANGED` entries.
The states are:

- `UNCHANGED`: still reported by its source and equal to the trusted hash.
- `CHANGED`: still reported and different from the trusted hash.
- `NEW`: currently monitored but absent from the baseline.
- `REMOVED`: a tracked file is absent from disk.
- `RESTORED`: a baseline `debsums` file is no longer reported as failed.

Review and explicitly replace the trust state:

```sh
sudo config-integrity update
sudo config-integrity update --yes
```

`update` displays differences first and asks for confirmation. `--yes` is
intended for a workflow in which the proposed state has already been reviewed.
Checks and package upgrades never update the baseline automatically.

All commands accept `--baseline PATH` and `--extra-files PATH`, which are
particularly useful in tests or isolated environments. See command-specific
`--help` output.

## Additional files

The optional `/etc/config-integrity/extra-files` contains one absolute path per
line. Blank lines and lines whose first non-whitespace character is `#` are
ignored. Directories are not scanned recursively.

```text
# Application configuration outside dpkg conffiles
/opt/my-app/appsettings.json
/srv/example/config with spaces.json
```

An existing extra file is recorded with source `extra`. A missing tracked extra
file is `REMOVED`; an existing newly listed file is `NEW`.

## Output and exit codes

Example anomaly:

```text
CHANGED /etc/nginx/nginx.conf [nginx-common]
  old: 0123...
  new: abcd...
Checked: 8 | Changed: 1 | New: 0 | Removed: 0 | Restored: 0
```

- `0`: successful initialization/update, or a clean check
- `1`: integrity differences found; also used when an update is declined
- `2`: operational or configuration error

This makes `check` suitable for cron and systemd monitoring.

## Design and security

`debsums -e` output from both stdout and stderr is accepted only when every
non-empty line unambiguously identifies an absolute path with an `OK` or
`FAILED` result. `OK` entries are understood and ignored; only `FAILED` paths
enter the monitor. Unrecognized output stops the scan instead of being silently
discarded. Exit statuses `0` (no discrepancy) and `2` (discrepancies found)
are accepted; other statuses are operational failures. This was verified with
Debian 13's `debsums` 3.0.2.3.

Files are opened without following a final-component symlink where the platform
supports `O_NOFOLLOW`. Symlinks and non-regular files are rejected. SHA-256 is
calculated incrementally, and device, inode, size, modification time, and
change time are compared before and after reading to catch common races. This
does not make filesystem inspection perfectly race-free, particularly where a
parent directory is attacker-controlled.

Package ownership is best-effort via `dpkg-query -S -- PATH`; failure to find an
owner does not prevent hashing. Baseline replacement uses a same-directory
temporary file, `fsync`, `os.replace`, and directory `fsync`.

## Limitations

- The tool inherits the coverage and accuracy of `debsums -e` for Debian files.
- It is not a general filesystem scanner, malware detector, remote attestation
  mechanism, or replacement for protected off-host audit records.
- A privileged attacker can alter the tool and its local baseline.
- File metadata, ownership, mode, ACLs, xattrs, and directory contents are not
  baselined; file contents are.
- A Debian file that stops being reported is called `RESTORED`, but the result
  should still be reviewed (for example after a package version change).
- Missing extra-file entries that were never in the baseline are not check
  anomalies; initialization/update refuses to trust such newly configured
  missing files. An already tracked missing file is reviewable as `REMOVED`
  during update and can then be explicitly removed from the baseline.

## Automated tests

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile config-integrity tests/test_config_integrity.py
```

The tests use temporary directories and fake subprocess results. They do not
need root, modify `/etc`, or require `debsums` to be installed.

## Scheduled checks with systemd

Example units are provided in `systemd/`. Install the current executable and
units, then enable the timer:

```sh
sudo install -o root -g root -m 0755 config-integrity /usr/local/sbin/config-integrity
sudo install -o root -g root -m 0644 \
  systemd/config-integrity.service /etc/systemd/system/config-integrity.service
sudo install -o root -g root -m 0644 \
  systemd/config-integrity.timer /etc/systemd/system/config-integrity.timer
sudo install -o root -g root -m 0644 \
  systemd/config-integrity.conf /etc/tmpfiles.d/config-integrity.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/config-integrity.conf
sudo systemctl daemon-reload
sudo systemctl enable --now config-integrity.timer
```

The timer runs daily, adds up to 15 minutes of randomized delay to avoid a
fixed load spike, and catches up after downtime with `Persistent=true`. It runs
only `check`; it never updates the trusted baseline.

Run and inspect a check immediately:

```sh
sudo systemctl start config-integrity.service
systemctl status config-integrity.service
sudo journalctl -u config-integrity.service --since today
systemctl list-timers config-integrity.timer
```

A clean check exits 0. Integrity differences exit 1 and operational errors exit
2, so either kind of problem marks that oneshot invocation failed while the
timer remains scheduled. The journal contains the state and summary needed to
distinguish them. For active notification, connect systemd's `OnFailure=` to an
email, webhook, or monitoring service appropriate for the host; the supplied
unit deliberately does not assume or embed notification credentials.

`PrivateTmp` is intentionally not enabled because an explicitly configured
extra file may live under `/tmp`. If the host never monitors such paths, an
administrator may add that hardening option in a local systemd override.

### Read-only result for a monitoring agent

The supplied service publishes a sanitized JSON result after every run at:

```text
/run/config-integrity/result.json
```

The systemd unit deliberately grants read access only to the `nmgroup` group:

```text
directory: root:nmgroup 0750
file:      root:nmgroup 0640
```

On this host `nmuser` is a member of `nmgroup`, so the processor agent can read
the result but cannot run the root check, alter the result, change the timer,
or modify the baseline. If the consumer uses a different group, update both
`nmgroup` occurrences in `systemd/config-integrity.conf` and the
`--result-group nmgroup` argument in `systemd/config-integrity.service` before
installing the files.

The result uses the stable `config-integrity-result/v1` schema. It includes a
timestamp, exit code, status, aggregate counts, and sanitized findings
(`state`, `path`, source, and package where known). It intentionally excludes
file contents and hashes. Its `consumer_guidance` array is intended for a later
LLM or automation consumer. It explains the meaning of each exit code and
finding state, the review required before trusting a change, the exact
administrator `sudo config-integrity update` and recheck workflow, how a
rejected update behaves, and the investigation path for operational errors.
The consumer may use that guidance to advise an administrator, but remains
read-only and cannot itself make a baseline-changing call.

Inspect the current result as the monitoring user:

```sh
sudo -u nmuser cat /run/config-integrity/result.json
```

For direct integrations, `check --json` emits the same sanitized schema and
`check --json --result-file PATH --result-group GROUP` atomically publishes it.

### Optional Ready For Quantum Network Monitoring integration

This is an optional integration feature. `config-integrity` works fully as a
standalone command-line and systemd-timer tool; installing Ready For Quantum
Network Monitoring is not required.

To make the published result available to Ready For Quantum Network Monitoring,
install both components on the monitored host:

1. Install and enable this project's root-owned `config-integrity.service` and
   `config-integrity.timer` as described above.
2. Install the Docker version of the Ready For Quantum Network Monitoring
   processor agent using the official [agent download page](https://readyforquantum.com/Download).
3. Install Docker Desktop/Engine with Docker Compose, create the agent state
   directory, and use this Compose configuration. The official image is
   multi-architecture (`amd64` and `arm64`).

   ```yaml
   services:
     networkmonitorprocessor:
       image: mungert/networkmonitorprocessor:latest
       container_name: processor
       user: root
       restart: always
       volumes:
         - ${HOME}/state:/app/state/
         - /run/config-integrity:/run/config-integrity:ro
   ```

   Create the state directory and start the agent:

   ```sh
   mkdir -p "$HOME/state"
   docker compose up -d
   ```

   The first volume is the documented persistent agent state. The second is
   the added, **read-only** configuration-integrity handoff.
4. Authorize the Docker agent. Use `docker logs processor -f`, open the
   device-authorization URL printed by the agent, complete sign-in, and wait
   for the successful authorization message. Then sign in to the
   [Ready For Quantum dashboard](https://readyforquantum.com/dashboard) with
   the same account and select the local agent as the monitoring location.
5. The read-only integrity-result mount is:

   ```text
   host path:      /run/config-integrity
   container path: /run/config-integrity
   mode:           read-only
   ```

The documented Docker agent runs as `root` **inside the container**, so it can
read this read-only mount. It still cannot modify the host result because the
mount is `:ro`, and it receives no mount of the baseline directory. For a
non-root container deployment, grant its process the corresponding host group
ID as a supplementary group: on this host the directory is
`root:nmgroup 0750` and the result is `root:nmgroup 0640`. Do not make either
world-readable.

The integration is intentionally one-way: the agent reads
`/run/config-integrity/result.json` and reports the sanitized status to the
monitoring system. It has no mount of `/var/lib/config-integrity`, no ability to
write the result, and no authority to run `init` or `update`. A future static
`configintegrity` endpoint in the processor agent can consume the
`config-integrity-result/v1` schema and present it as a normal monitored
endpoint.
