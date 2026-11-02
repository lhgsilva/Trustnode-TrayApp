# TrustNode Link — prototype

Two small exes that let an engineer anywhere reach the devices on the
machine network where a TrustNode Edge PC sits.

**Prototype. Not wired into TrustNode.** Nothing in the shipped app imports
this directory.

```
prototypes/tnlink/
  tnlink.py          both roles, one file, stdlib + cryptography only
  test_tnlink.py     self-test, drives both roles over a real socket
  build.py           produces the two exes and asserts its own output
  dist/
    tnlink-server.exe   11.4 MB   runs on the PC on the PLC network
    tnlink-client.exe   11.4 MB   runs on the engineer's PC
```

## Two modes

**Relay mode (default) - works between any two computers with internet.**
Both ends dial OUT to a rendezvous, so neither needs an open port, a
port-forward, a firewall rule or administrator rights. The engineer types
only a Connection Code; the relay address is baked into the exe.

```
  plant PC  --outbound-->  relay (your VPS)  <--outbound--  engineer
  (agent)                  pairs by room id,
                           then a dumb byte pipe
```

The relay sees a 16-byte room id (`HMAC-SHA256(code)[:16]`), the two IP
addresses and byte counts. It **cannot read the traffic** - the X25519 +
ChaCha20-Poly1305 handshake runs end to end through it, confirmed by an
HMAC keyed with the Connection Code. `test_relay.py` proves this by
capturing every byte the relay forwards and asserting the plaintext is not
among them. A compromised relay leaks metadata, not data.

Relay setup is in [deploy/](deploy/). Override with `--relay host:port`.

**Direct mode (`--direct`) - same LAN only.** The server listens and the
client connects to it. Needs an inbound firewall rule, which the server
adds itself when run as administrator. Useful on a bench; not deployable at
a customer site.

## Running it

**Double-click both.** No command line needed.

### On the PC that sits on the PLC network

Double-click `tnlink-server.exe`. It prints one line to hand over:

```
    GIVE THIS ONE LINE TO THE ENGINEER:

        192.168.0.161:51820#TN-ASMUW-XWB2Z-X6ZC2
```

Underneath it lists this PC's other addresses, each with the adapter it
belongs to, and marks the ones that are virtual:

```
  If that address does not work, this PC is also on:
    192.168.10.110  ASIX AX88772B USB2.0 to Fast Ether  machine/second network
    192.168.56.1    VMware Virtual Ethernet Adapter fo  virtual adapter - usually
                                                        NOT reachable from another PC
```

That ranking exists because of a real mistake: shown five bare addresses,
the operator picked the VMware host-only one, which exists only inside that
PC, and the client sat trying to reach something unreachable. The address
on the **GIVE THIS ONE LINE** row is the one Windows itself uses to reach
the outside world.

### On the engineer's PC

Double-click `tnlink-client.exe` and paste that line:

```
  Paste here : 192.168.0.161:51820#TN-ASMUW-XWB2Z-X6ZC2
```

An address on its own also works - it then asks for the Connection Code
separately. The paste survives whitespace, lower case, missing hyphens, and
`#`, `|` or `/` as the separator.

### Then

| Command | What it does |
|---|---|
| `info` | the server's networks, which adapter each is, what is reachable, time left |
| `ping <ip> [count]` | ICMP echo issued from the server's own stack - real RTT and TTL |
| `scan <cidr> [ports]` | sweep up to a /24 to find PLCs, ifm blocks, meters |
| `map [bind:]lport ip:rport` | forward a local TCP port to a remote device:port |
| `maps`, `help`, `quit` | |

Example - reach a Logix controller's CIP port from a laptop:

```
tnlink> scan 192.168.10.0/24 44818
tnlink> map 44818 192.168.10.10:44818
  127.0.0.1:44818  ->  192.168.10.10:44818
```

...then in FactoryTalk Linx / RSLinx use the **Ethernet devices (static IP
list)** driver and enter `127.0.0.1`.

**The window never closes on its own.** Any error - wrong code, unreachable
address, port in use - is printed and the window waits for Enter. Piped or
scripted runs are not a tty, so they still exit immediately and can never
hang.

Server options: `--port`, `--bind`, `--code`, `--minutes` (session lifetime,
default 60, `0` = no limit), `--allow CIDR` (repeatable; adds to the
allowlist, which otherwise defaults to the PC's own private subnets).

## Commands

Type `help` for the list, `help <command>` for detail and examples.

| Command | What it does |
|---|---|
| `devices` | **start here** - scans the server's networks and numbers what answers, so later commands take `1` instead of an address |
| `ping <ip or #> [count]` | ICMP from the **server's** network, so it proves the device answers *there* |
| `map <lport> <ip or #>:<port>` | forward a local port; point any Windows program at `127.0.0.1:<lport>` |
| `rdp [ip or #]` | Remote Desktop to the plant PC, on a port picked for you, and prints the exact `mstsc` line |
| `claim <ip or #>` | make the device answer at its **real** address on this PC (admin) |
| `unclaim` | remove claimed addresses |
| `scan <cidr> [ports]` | scan one range |
| `maps`, `status`, `info` | what is set up |

### Why `rdp` exists instead of `map 3389`

Your own PC almost certainly runs Remote Desktop, so `3389` is already
bound locally and the forward cannot be created. Measured on the machine
where this was reported: `TermService` held `0.0.0.0:3389`. `rdp` picks a
free port (13389 upward) and tells you what to type. Verified end to end:
a real RDP server answered through the tunnel with `RDP_NEG_RSP`.

### `claim` - the device at its real address, without a driver

`claim 192.168.10.240` adds that address to this PC as a `/32` and
forwards its common ports, so software already configured for
`192.168.10.240` reaches the real device with **no reconfiguration**.

- Needs administrator rights.
- `store=active`: the address disappears at the next reboot even if this
  program is killed. (`New-NetIPAddress` also writes the persistent store,
  which would hijack that address on the engineer's laptop forever.)
- **Refused** if this PC is already on that subnet - claiming would then
  hijack a real device on your own network.
- **TCP only.** Windows `ping` to a claimed address is answered by your own
  stack and tells you nothing; use this tool's `ping` for the real device.

## What this does NOT do, and why

**It does not create a virtual network adapter.** So Windows' own `ping.exe`
and Studio 5000's *broadcast* EtherNet/IP browse do not transparently see
the remote subnet.

For an unmodified program's packets to be routed into a tunnel, the OS must
own a network interface that leads there. On Windows that means a
**signed kernel-mode TUN driver** — there is no userspace way to capture
another process's traffic. So "no third-party software at all" and "the OS
`ping` command works transparently" cannot both be true in one small exe.

This prototype takes the half that needs nothing installed: the client gets
ping, discovery and TCP forwarding, which is enough to find the devices and
to go online with Studio 5000 using the static-IP driver.

### If transparent same-network behaviour is required later

Two routes, both of which change the "no third-party" answer:

1. **Wintun** (WireGuard's driver). Loaded from a DLL at runtime by an
   elevated process, so a *portable* exe still works — no MSI. Needs its
   licence cleared before shipping commercially.
2. **Windows' own built-in VPN client** (IKEv2/SSTP). No driver ships at
   all, because Windows already has one — but the plant side then has to
   speak a standards-compliant VPN protocol, which is a large job.

Only the **engineer's** PC needs this. The plant PC never does: it reaches
the PLC with ordinary sockets from an address the PLC already talks to, so
there is no driver, no IP forwarding, no NAT, and no change to the PLC's
gateway settings. That asymmetry is the main design decision here and it is
what keeps the OT side untouched.

## Security

- **X25519 ECDH per connection**, confirmed by HMAC-SHA256 over the full
  handshake transcript keyed with the Connection Code. A session key is
  never reused; an eavesdropper cannot replay; a man in the middle cannot
  sit in it without the code.
- **ChaCha20-Poly1305** AEAD, separate key and counter per direction. Every
  frame is authenticated. A forged frame drops the link — and the server
  keeps serving, which is a bug the self-test found and now guards.
- **Allowlist.** The server refuses any target outside it, defaulting to its
  own directly-connected private subnets. Loopback, multicast and
  link-local are always refused. Without this the plant PC would be an open
  proxy to the internet.
- **The session expires** and the server exits.
- **Every operation is logged** with a timestamp: pairing, pings, scans,
  each TCP open and close, and every refusal.

Known prototype limits, deliberately:

- **It listens for an inbound TCP connection**, so the first test needs a
  LAN or a port-forward. Production must be outbound-only from the plant,
  via a rendezvous on the VPS — that is the next step, not this one.
- One client at a time.
- The Connection Code is generated per run and is not yet issued by the
  portal. It should be, under the customer's tenant, reusing the existing
  `tenant → customer → licence → edge` chain rather than a second identity
  system.
- No UI. Production belongs behind an operator-present **Enable** button
  with a duration, in Connections.

## Tests

```
python prototypes/tnlink/test_tnlink.py
```

Covers code normalisation (whitespace, case, em dashes, line wrap, missing
prefix — and that a *different* code still fails), ICMP returning real
measurements, wrong-code refusal, the allowlist refusing both ping and TCP,
200 KB surviving the framing intact, and a forged frame dropping the link
without killing the server.

Measured on 2026-10-21, both exes, against a real LAN device: pair from a
code typed as `tn test1 test2 test3`, ping 3/3 at 2–3 ms TTL 64, and a /29
scan that found the device with ports 80 and 443 open.
