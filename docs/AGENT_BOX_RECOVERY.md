# agent-box recovery contract

This document separates process liveness from real remote reachability for the two recovery paths on `agent-box`.

## Normal path

Use DevSpace for routine repository work. Desktop Commander is break-glass only and should not be used while DevSpace is healthy.

A healthy DevSpace claim requires more than a running process or public HTTP listener: open the exact project root and perform a real tool call through the same workspace. A public `/mcp` HTTP 401 without Authorization is only an edge/auth sanity check, not an end-to-end health result.

## Desktop Commander health layers

Treat these as distinct states:

1. `systemd` process state: `desktop-commander-remote.service` is running.
2. outbound control-plane state: `https://mcp.desktopcommander.app/api/mcp-info` is reachable over IPv4.
3. remote registration state: the expected `agent-box` device is shown online by the Desktop Commander connector.
4. end-to-end state: a connector `ping` to the exact device ID returns `pong`.

Only layer 4 is a successful recovery. Never report Commander healthy from `systemctl is-active` alone.

## Refresh-token rotation incident

Desktop Commander 0.2.48 rotates Supabase refresh tokens during `refreshSession()`. The tested upstream device process persisted the session only during startup, so a later service restart could replay an already-consumed refresh token and fail with `Invalid Refresh Token: Already Used`.

`bin/desktop_commander_compat.py` is an exact-build guard for that incident. It patches only the tested 0.2.48 `device.js` SHA-256 and fails closed on another version or build. The patch serializes config writes, atomically replaces `device.json`, and persists every `TOKEN_REFRESHED` session immediately. It never prints tokens.

After a Desktop Commander upgrade, run the guard in `status` mode first. Do not force the old patch onto a new build; update the tested hashes and focused tests instead.

## Recovery order

When Commander is offline but DevSpace works, diagnose and repair through DevSpace first. Do not reboot the VM and do not reset/logout Tailscale for a Commander-only incident.

If logs contain `Invalid Refresh Token: Already Used`, one interactive device authorization may be required to mint a new session. After approval, verify the exact remote device with connector `ping`; then install the compatibility patch so subsequent token rotations are persisted.

If both DevSpace and Commander are down, use the cloud-provider console/serial or browser SSH as the out-of-band path. The OOB path must remain independent of the laptop, DevSpace, Commander, and Tailscale. Keep provider IAM access and the VM instance identity documented outside the VM; do not store auth tokens or session secrets in this repository.

## Laptop-off requirement

`agent-box` recovery must not depend on the user's laptop being powered on. DevSpace, Commander, systemd, and the OOB cloud console all terminate on or reach the VM directly. A laptop-local tunnel, watchdog, or credential broker must never be the only restart/recovery mechanism.
