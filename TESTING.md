# Testing

## Automated suite

From the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile config-integrity tests/test_config_integrity.py
./config-integrity --help
./config-integrity init --help
./config-integrity check --help
./config-integrity update --help
```

The unit tests replace command execution with controlled results and use only
temporary directories. They therefore do not require root or change the host's
Debian configuration.

## Manual Debian 13 integration test

Perform this on an appropriate test host or with suitable change control.

### Installation prerequisite

```bash
sudo apt install debsums
debsums --version
```

### Establish an existing modified configuration

Use an already intentionally customized package-managed configuration if one
is available:

```bash
sudo debsums -e
```

A result may resemble:

```text
/etc/nginx/nginx.conf                                                     FAILED
```

Do not modify an important production nginx file merely to create a test case.

### Initialize

```bash
sudo ./config-integrity init
```

Verify the baseline exists, is root-owned and restrictive, and includes known
`debsums -e` failures:

```bash
sudo stat /var/lib/config-integrity /var/lib/config-integrity/baseline.json
sudo python3 -m json.tool /var/lib/config-integrity/baseline.json
```

The expected modes are `0700` for the directory and `0600` for the baseline.
Then run:

```bash
sudo ./config-integrity check
echo $?
```

Expect no changes and exit status 0.

### Safe change test with an extra file

Prefer the extra-files feature to changing an important packaged file:

```bash
printf '%s\n' 'initial test value' | sudo tee /tmp/config-integrity-test.conf >/dev/null
sudo install -d -o root -g root -m 0755 /etc/config-integrity
printf '%s\n' '/tmp/config-integrity-test.conf' | sudo tee /etc/config-integrity/extra-files >/dev/null
sudo ./config-integrity update
```

Review and accept the proposed `NEW` entry. Alter the temporary file and check:

```bash
printf '%s\n' 'changed test value' | sudo tee /tmp/config-integrity-test.conf >/dev/null
sudo ./config-integrity check
echo $?
```

Expect `CHANGED /tmp/config-integrity-test.conf` and exit status 1.

### Removal test

```bash
sudo rm /tmp/config-integrity-test.conf
sudo ./config-integrity check
echo $?
```

Expect `REMOVED /tmp/config-integrity-test.conf` and exit status 1. The removal
is deliberate and confined to the disposable test file.

### New file test

Restore and trust the first file, then add another path only after that baseline
has been accepted:

```bash
printf '%s\n' 'restored test value' | sudo tee /tmp/config-integrity-test.conf >/dev/null
sudo ./config-integrity update
printf '%s\n' 'second file' | sudo tee /tmp/config-integrity-new.conf >/dev/null
printf '%s\n' '/tmp/config-integrity-new.conf' | sudo tee -a /etc/config-integrity/extra-files >/dev/null
sudo ./config-integrity check
echo $?
```

Expect `NEW /tmp/config-integrity-new.conf` and exit status 1.

### Update behavior

```bash
sudo ./config-integrity update
```

Verify that it displays changes before updating and asks for confirmation.
Answer `n`, then confirm that the baseline timestamp/content did not change.
Run it again and answer `y`. Afterwards:

```bash
sudo ./config-integrity check
echo $?
```

The check should be clean with exit status 0.

### debsums-specific comparison

```bash
sudo debsums -e
sudo ./config-integrity check --verbose
```

Compare the FAILED paths with the entries whose source is `debsums`. Confirm a
real line such as `/etc/nginx/nginx.conf FAILED` is discovered correctly. Also
record whether the installed Debian 13 `debsums -e` returns 0 with no failures
and 2 with failed files, and whether it emits any additional diagnostic lines.
Debian 13 `debsums` 3.0.2.3 is known to emit both `OK` and `FAILED` lines; both
forms are understood, while only `FAILED` paths enter the baseline.

### unattended-upgrades scenario

1. Run `sudo ./config-integrity check`.
2. Allow normal unattended upgrades, or perform a legitimate package upgrade.
3. Run `sudo ./config-integrity check` again.
4. Review every `NEW`, `CHANGED`, `REMOVED`, or `RESTORED` state.
5. Verify each difference corresponds to the legitimate package operation.
6. Run `sudo ./config-integrity update` only after that review.

The tool intentionally does not trust package upgrades automatically.

### systemd timer test

After installing the example units as described in `README.md`, verify their
syntax and schedule, then start an immediate check:

```bash
systemd-analyze verify systemd/config-integrity.service systemd/config-integrity.timer
sudo systemctl daemon-reload
sudo systemd-tmpfiles --create /etc/tmpfiles.d/config-integrity.conf
sudo systemctl enable --now config-integrity.timer
sudo systemctl start config-integrity.service
systemctl status config-integrity.service --no-pager
sudo journalctl -u config-integrity.service -n 20 --no-pager
systemctl list-timers config-integrity.timer --no-pager
```

Expect the service invocation to finish successfully when the baseline is
clean. The oneshot service normally becomes `inactive (dead)` after success;
the timer remains `active (waiting)`. A detected difference makes the service
invocation fail with status 1 but does not disable future timer runs.

### Read-only result handoff test

The supplied service publishes a sanitized JSON status for the unprivileged
Network Monitor processor agent. On the current host, verify its access as
`nmuser`:

```bash
sudo systemctl start config-integrity.service
sudo stat -c '%U:%G %a %n' /run/config-integrity /run/config-integrity/result.json
sudo -u nmuser python3 -m json.tool /run/config-integrity/result.json
```

Expect `root:nmgroup` with modes `0750` and `0640`. Confirm that the JSON has
schema `config-integrity-result/v1`, contains `consumer_guidance` with the
administrator update and recheck workflow, contains no file contents or
SHA-256 values, and has an exit code consistent with the service invocation.
The consuming agent must only read this file; it may advise an administrator
using the supplied guidance but must not be granted permission to invoke
baseline-changing commands.

### Optional Ready For Quantum Docker-agent handoff

This is not required for normal operation. After installing the Docker version
of the Ready For Quantum Network Monitoring processor agent from the official
[download page](https://readyforquantum.com/Download), mount the host result
directory into the container read-only at the same path:

```text
/run/config-integrity:/run/config-integrity:ro
```

Confirm from the agent container that it can read
`/run/config-integrity/result.json` but cannot create, replace, or alter it.
Do not mount `/var/lib/config-integrity` into the container. The agent should
consume only the `config-integrity-result/v1` result file and use its
`consumer_guidance` when explaining a finding to an administrator.

### Cleanup of disposable example files

After testing, remove the two `/tmp/config-integrity-*.conf` entries from
`/etc/config-integrity/extra-files`, run and review `update`, then remove the
disposable files if still present.
