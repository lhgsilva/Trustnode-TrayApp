# Deploying the TrustNode Link relay

The relay is what lets the prototype work between any two computers with
internet access. Both ends dial **out** to it, so neither needs an open
port, a port-forward, a firewall rule or administrator rights.

It is one stdlib Python file, no dependencies.

## What it does and does not see

| | |
|---|---|
| **Sees** | a 16-byte room id = `HMAC-SHA256(Connection Code)[:16]`, the two IP addresses, byte counts |
| **Never sees** | the Connection Code, and **not one byte of plaintext** |

The agent and the client run their X25519 + ChaCha20-Poly1305 handshake end
to end *through* the relay, confirmed by an HMAC keyed with the Connection
Code. A relay that tried to man-in-the-middle would have to produce that
HMAC, which it cannot. `test_relay.py` section 6 proves this by capturing
every byte the relay forwards and asserting the plaintext is not in it.

So a compromised relay leaks **metadata, not data**.

## Install

```bash
# as root on the VPS
adduser --system --group --no-create-home tnlink
mkdir -p /opt/tnlink
# copy tnlink_relay.py to /opt/tnlink/
install -m 0644 tnlink-relay.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tnlink-relay
systemctl status tnlink-relay --no-pager
```

Open the port:

```bash
ufw allow 7443/tcp comment 'TrustNode Link relay'
```

Check it:

```bash
journalctl -u tnlink-relay -f
```

## The provider firewall is a SECOND firewall

Measured 2026-10-21 on the live VPS (AlmaLinux 9.7, 87.106.7.67):

```
  22    OPEN
  80    OPEN
  443   OPEN
  7443  filtered   <- silently dropped
```

The service was active, listening on `0.0.0.0:7443`, firewalld had the port
open, and a connection from the VPS to itself worked. It still could not be
reached from outside, because **IONOS has a cloud firewall in front of the
machine** and it is configured in their web panel, not over SSH.

Open it at: IONOS Cloud Panel -> Network -> Firewall Policies -> the policy
on this server -> Add rule: TCP, port 7443, inbound, any source.

Opening a port on the host is not the same as opening it to the internet.
Always test from outside, never from the VPS itself.

## Choosing the port

`7443` by default. Most home and office networks allow arbitrary outbound
TCP, so this is fine.

**Strict corporate networks often allow outbound 443 only.** If you hit
that, move the relay to 443 — but 443 is already taken by the portal's
nginx, so that needs one of:

- a second IP address on the VPS, or
- nginx `stream` on a second address, or
- a TLS-ALPN split (more work, not prototype material).

Plan for it: the port is the single most likely reason a customer site
cannot connect, and it is worth testing from the customer's network early.

## Hardening already applied

The unit runs as an unprivileged user with `ProtectSystem=strict`, no
capabilities, no writable paths, `MemoryDenyWriteExecute`, and limits on
memory, tasks and file descriptors. The relay itself caps concurrent rooms
(`MAX_ROOMS = 200`) and reaps registrations idle for over a day.

## Before this is a product

- **The room id is the only thing the relay checks.** Anyone who guesses a
  room can occupy it and deny service to that code - they still cannot
  decrypt anything or pair, but they can be a nuisance. Production should
  have the portal issue Connection Codes and the relay verify a short-lived
  token against it, reusing the existing
  `tenant -> customer -> licence -> edge` chain.
- **No rate limiting.** Add per-IP connection limits before exposing it
  widely.
- **No TLS on the relay hop.** It is not needed for confidentiality - the
  payload is already end-to-end encrypted - but TLS would hide the room id
  from anyone watching the network, and would let the relay live behind
  nginx on 443.
