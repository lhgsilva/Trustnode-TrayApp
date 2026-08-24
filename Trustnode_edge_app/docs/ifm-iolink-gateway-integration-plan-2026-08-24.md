# Adding IFM IO-Link masters as a gateway type — investigation and plan

**Date:** 2026-08-24
**Goal:** read the inputs of sensors hanging off an IFM EtherNet/IP IO-Link master
block (AL1326 and family), turn them into normalized TrustNode tags, and collect
them on the gateway interval so they trend, chart, report and batch exactly like
Allen-Bradley or Siemens tags do today.
**Hard constraint:** the four existing gateway types must behave *identically*
after this change. Everything here is additive.

---

## 0. The short version

The block speaks two protocols, and the interesting one is not EtherNet/IP.

* **EtherNet/IP (CIP)** — how the block talks to a PLC. Cyclic implicit I/O plus
  explicit messaging. Needs an EDS file, an assembly-instance map, and a CIP
  stack. This is the *right* answer if the edge must behave like a PLC.
* **IoT Core (HTTP + JSON)** — a second, independent Ethernet port (X23 on the
  AL1326) that exposes every port's process data, the master's own diagnostics
  and the connected sensors' identity as a JSON tree over plain HTTP. No
  fieldbus configuration at all.

**We should integrate over the IoT Core.** It is a request/response HTTP API,
which maps onto our existing "poll every interval, return a list of readings"
driver contract with no new runtime, no binary protocol stack, and no EDS
tooling. It is also the port ifm designed for exactly this purpose — the block
can stay wired to the customer's PLC over EtherNet/IP at the same time, and we
read the same data without touching that control path.

The one real piece of engineering is that `pdin` returns **raw hex**, not
engineering units. Turning `"03C9"` into `24.2 °C` is our job. That decoding
layer is the heart of the new library.

---

## 1. What the hardware is

An IFM IO-Link master is a field block (IP67) with:

* **8 × M12 IO-Link ports** (AL1326) — each port takes one IO-Link sensor, or a
  plain digital input/output.
* **2 × EtherNet/IP ports** (X21/X22) — the fieldbus side, to the PLC.
* **1 × IoT port** (X23) — an independent Ethernet interface serving the IoT
  Core JSON API.
* 16 configurable digital inputs / 8 digital outputs, 20–30 V DC.

The AL13xx family (AL1320/21/22/26, AL1330/50, AL1372…) shares the IoT Core API,
so a driver written against the API covers the range, not just one part number.
Whether a given unit exposes the IoT Core on a *separate* port or on the same
interface varies by model — the driver must not assume.

**Vendor ID 310 = ifm**, which is how we recognise ifm's own sensors when
offering decode profiles.

---

## 2. The IoT Core API, concretely

Verified against ifm documentation, the Cybus integration guide and a working
open-source Python client (see Sources).

### Read one datapoint

```
GET http://<ip>/iolinkmaster/port[1]/iolinkdevice/pdin/getdata
```

```json
{ "cid": 4711, "data": { "value": "03C9" }, "code": 200 }
```

### The same thing as JSON-RPC (POST to `/`)

```json
{ "code": "request", "cid": 4711,
  "adr": "/iolinkmaster/port[1]/iolinkdevice/pdin/getdata" }
```

### Read many datapoints in ONE request — this is the one that matters

`getdatamulti` reads a list of datapoints and returns a value *and a per-point
diagnostic code* for each. At a 1 s interval across 8 ports, this is the
difference between 8+ HTTP round trips per cycle and one. **The driver must use
`getdatamulti` as its normal path**, falling back to per-port `getdata` only
when a master rejects it.

### Datapoints we care about

| Path | Meaning |
|---|---|
| `/iolinkmaster/port[N]/iolinkdevice/pdin` | process input, hex string |
| `/iolinkmaster/port[N]/iolinkdevice/pdout` | process output, hex |
| `/iolinkmaster/port[N]/iolinkdevice/productname` | sensor product name |
| `/iolinkmaster/port[N]/iolinkdevice/vendorid`, `/deviceid` | identity, for profile matching |
| `/iolinkmaster/port[N]/pin2in` | the port's plain digital input |
| `/processdatamaster/temperature`, `/current` | the master's own health |
| `/deviceinfo/productcode`, `/serialnumber` | block identity |
| `/devicetag/applicationtag` | operator-assigned block name |

### Other facts that shape the design

* **Auth:** none by default on HTTP. Newer firmware supports HTTPS on 443 with
  Basic Auth (`administrator`). The driver takes optional credentials and an
  optional "verify TLS" switch — self-signed certificates are normal here.
* **Subscriptions:** the API can push on `datachanged` to a callback URL. We
  will **not** use this initially: it inverts control flow, needs an inbound
  listener, and our whole pipeline is built around "the gateway interval drives
  the sample". Polling `getdatamulti` at the configured interval keeps IFM tags
  on exactly the same cadence as every other tag, which is what makes them
  comparable on a chart. Worth revisiting only for sub-100 ms needs.
* **Diagnostics:** a per-point code that isn't 200 means that port failed. That
  maps onto our existing quality model rather than being swallowed.

---

## 3. The decoding problem, and how we solve it

`pdin` is a hex string whose layout is defined by each sensor's **IODD** (IO
Device Description). ifm's documented example: bits 2–15 carry a temperature,
resolution 0.1 °C, so `0b11110010` = 242 → **24.2 °C**.

Three possible approaches:

1. **Parse IODD XML files.** Most "correct", by far the most work: IODD is a
   large schema, files must be sourced per sensor from IODDfinder, and we would
   be shipping a parser plus a file store for a first release.
2. **Hard-code per-sensor decoders.** What the open-source client does (vendor
   310, device 416 → acceleration = `int(hex[0:4],16)/100`). Fast to write,
   silently wrong for any sensor nobody hard-coded.
3. **A declarative bit-field mapping the operator can see and edit, plus
   built-in profiles for common sensors.** ← **chosen**

Each IFM tag is one field:

```jsonc
{ "name": "Temp_Tank1",   // becomes the TrustNode tag name
  "port": 3,
  "bit_offset": 2,        // from the MSB of pdin, per IODD convention
  "bit_length": 14,
  "kind": "int",          // uint | int | bool | float32
  "scale": 0.1,           // engineering value = raw * scale + offset
  "offset": 0.0,
  "unit": "degC" }
```

This is honest about what it is doing, it is inspectable when a value looks
wrong, and it covers *any* sensor without us shipping a parser. Built-in
profiles (a named set of fields for, say, an ifm TA2105 temperature sensor) make
the common case one click, and IODD import can be added later behind the same
data structure without changing anything downstream.

**Bit convention:** IODD numbers bits from the MSB of the process data. Getting
this backwards is the single most likely source of "the number is nonsense", so
the library implements it once, documents it, and tests it against ifm's own
worked example (242 → 24.2 °C).

---

## 4. How it plugs into TrustNode

The existing contract is small, which is what makes this safe:

* `GatewayType` — a `Literal` in `backend/app/models.py`.
* `GatewayConfig` — one flat model; `tags: List[str]`.
* `_read_from_gateway()` in `plc_manager.py` — an if/elif on `gateway_type`
  returning `List[GatewayReading]`.
* `GatewayReading` — `ts_utc, tag_name, value, value_text, data_type, quality…`.

Everything downstream (historian, distribution, charts, dashboards, batches,
reports, triggers) consumes `GatewayReading` and knows nothing about protocols.
**So a new gateway type that returns well-formed `GatewayReading`s inherits the
entire product for free.** That is the whole integration strategy.

### The changes, each additive

| # | File | Change | Risk |
|---|---|---|---|
| 1 | `backend/app/drivers/ifm_iolink.py` *(new)* | The library: transport, decode, profiles. Pure, no imports from plc_manager. | none — new file |
| 2 | `models.py` | Add `"ifm_iolink"` to the `GatewayType` Literal | none — widening a union |
| 3 | `models.py` | Add `ifm_ports: List[dict] = []` to `GatewayConfig` | none — defaulted field |
| 4 | `plc_manager.py` | One `elif gateway_type == "ifm_iolink"` branch | none — unreachable for existing types |
| 5 | `plc.py` | `discover_tags` branch → scan the block's ports | none — new branch |
| 6 | `plc.py` / auto-resume | Pass `ifm_ports` into `GatewayConfig` | low — one new kwarg |
| 7 | `App.jsx` | New entry in `gatewayOptions`, conditional fields in the modal | low — gated on the new type |

`tags: List[str]` stays exactly as it is: an IFM tag's *name* is the operator's
own (`Temp_Tank1`), and the port/bit mapping lives in `ifm_ports`. So the tag
name that reaches the historian, a dashboard widget or a report is a normal
name — indistinguishable from a PLC tag, which is precisely the requirement.

---

## 5. The device/gateway dialog

Today the modal shows PLC IP, OPC-UA URL and a free-text tag list, switched on
gateway type. For IFM it needs a different shape, without disturbing the
existing one:

* Block address, optional IoT port, optional credentials.
* **Scan ports** — one call that walks ports 1..N and reports what is plugged in
  (product name, vendor/device id, whether a profile matches).
* A per-port tag table: enable a port, pick a profile *or* add fields by hand,
  each row showing name / offset / length / type / scale / unit and a **live
  decoded preview** next to the raw hex.

The live preview is the feature that makes this usable: an operator can see
`03C9 → 24.2 °C` before saving, which turns bit-offset guesswork into something
verifiable. Everything is rendered only when `gateway_type === "ifm_iolink"`, so
the Allen-Bradley and OPC-UA paths render byte-for-byte the same markup as now.

---

## 6. What could go wrong, and the guard for each

| Risk | Guard |
|---|---|
| A slow/unreachable block stalls the collection cycle | Hard HTTP timeout below the gateway interval; a failed cycle returns BAD-quality readings, never blocks. Same shape as the Snap7 timeouts. |
| Wrong bit offsets produce plausible nonsense | Live decode preview in the dialog; the raw hex is always available as its own optional tag. |
| A sensor is unplugged mid-run | Per-point diagnostic code ≠ 200 → that tag reads BAD quality with the reason, and the other ports keep working. |
| 8 ports × 1 s = HTTP storm | `getdatamulti`: one request per cycle regardless of port count. |
| Firmware without `getdatamulti` | Detect once, fall back to per-port `getdata`, log which path is in use. |
| A regression in existing gateways | The new code is a separate module reached only by a new enum value; existing tests (`test_collection_trigger`, the release gate, the soak) must pass unchanged. |

---

## 7. Delivery order

1. **The library** — `ifm_iolink.py` + unit tests, including ifm's worked
   example. No product wiring; provably correct in isolation.
2. **Backend wiring** — enum, config field, dispatch branch, discovery.
3. **Frontend** — gateway option, IFM section in the dialog, port scan, preview.
4. **Verification** — a fake IFM master (an HTTP fixture serving real response
   shapes) driven end-to-end: configure → start → readings land in the historian
   → a dashboard widget renders them. Then the existing suites, unchanged.

Field validation against a real AL1326 is the last step and needs the hardware;
everything above is testable without it, and the fake master is built from the
documented response shapes so the gap is small.

---

## Sources

- [ifm AL1326 product page](https://www.ifm.com/us/en/product/AL1326) and
  [AL1326 datasheet (PDF)](https://media.ifm.com/dam/78060517-8f99-46d8-b6ee-5ff72dd03a38/Original/AL1326-00_EN-GB.pdf)
- [Cybus — Connecting an ifm IO-Link Master](https://docs.cybus.io/2-4-2/guides/machine-connectivity/robots-sensors-shop-floor-devices/connecting-an-ifm-io-link-master)
  — request/response shapes, auth, polling
- [corlina/BF-001-IFM-PYTHON `daemon3.py`](https://github.com/corlina/BF-001-IFM-PYTHON/blob/master/daemon3.py)
  — a working Python client; endpoint paths and per-sensor hex decoding
- [iolink-ifm-master-python (FH Aachen)](https://git.fh-aachen.de/io-link/iolink-ifm-master-python)
- [ifm AL1350 operating instructions](https://www.manualslib.com/manual/1920343/Ifm-Al1350.html)
  — IoT Core services (`getdatamulti`, `subscribe`)
- [United Manufacturing Hub — ifm retrofitting / sensorconnect](https://umh.docs.umh.app/docs/features/connectivity/other-tools/ifm-retrofitting/)
  — prior art for exactly this pattern

**Note on the shared ChatGPT link:** it renders client-side, so only its title
was retrievable — *"Connect AL1326 Python"*. That named the part number, and the
research above was built from primary sources instead. If that conversation
contains specific decode tables or firmware notes, paste the text and I will
fold it in.
