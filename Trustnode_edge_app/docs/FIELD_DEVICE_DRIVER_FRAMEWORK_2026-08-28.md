# Field-device drivers — one way to add any device, on any Ethernet protocol

**Date:** 2026-08-28
**Asked for:** *"a Modbus TCP driver and an EtherCAT driver … the same for drives and VSDs of the PowerFlex and Kinetix family … or any other field device with Modbus TCP or EtherNet/IP that can communicate with us via an Ethernet cable … all these driver pages need to be global for any type of device in the industry … using how other professional software adds field devices — Ignition drivers, Studio 5000 and others."*

---

## 1. What professional systems actually do

Three products, one shape:

| Product | Layer 1 | Layer 2 | Layer 3 |
|---|---|---|---|
| **Ignition** | *Device Connection* — pick a driver, enter IP/port | driver exposes an OPC **tag tree** | browse or import tags |
| **KEPServerEX** | *Channel* (protocol + NIC) | *Device* (model profile) | tag groups, from a model template |
| **Studio 5000** | *Module* on a network | **AOP / EDS** knows the catalogue number | connection + parameter tags appear |

The common pattern is **Connection → Device profile → Tags**, where the *profile* is what turns a raw address space into named, typed, scaled tags. None of them writes a bespoke page per manufacturer; they ship a **profile library** and an **importer** (EDS, GSDML, CSV) that feeds one generic editor.

TrustNode already has the same three layers — **Devices → Gateways → Tags**. What is missing is the profile library and one generic editor behind it.

**Also worth stating plainly, because it shapes everything below: no SCADA on this list has a PROFINET or EtherCAT driver.** Ignition's driver list is Allen-Bradley (CIP), Siemens (S7comm), Modbus, DNP3, BACnet, Omron, and an OPC UA client. For PROFINET/EtherCAT devices, every one of these products reads the data *from the PLC that owns them*, or through a gateway appliance.

---

## 2. What we already have (and it is most of it)

| Asset | Where | Reused for |
|---|---|---|
| CIP explicit messaging client | `drivers/ethernet_ip.py` — `EipDeviceClient.read_signals()` | **PowerFlex, Kinetix, any EtherNet/IP device** |
| EDS parser + assembly guesser | `parse_eds`, `guess_assemblies`, `signals_from_eds` | Studio-5000-style "import the profile" |
| Byte/bit/scale decoder | `EipSignal`, `decode_signal` | every protocol's raw→engineering step |
| Device discovery | `discover_devices()` (CIP ListIdentity broadcast) | "scan the network" |
| Modbus TCP plumbing | `power_manager.py` — client cache, block-merge planner, per-register backoff | the new generic Modbus driver |
| Supplier table importer | `meter_registers.parse_supplier_table()` | any vendor register list, not just meters |
| Model profile library | `meter_registers.METER_MODELS` | the shape the global library copies |
| Per-tag collection ticks | added 2026-08-28 | keeps big profiles affordable |

**The EtherNet/IP driver is already generic.** It was written for an ifm block but it speaks plain CIP to an input assembly — which is exactly how a PowerFlex or Kinetix is read. Those need *profiles*, not a new driver.

---

## 3. What is genuinely missing

1. **Modbus TCP is locked inside `power_manager`.** `pymodbus 3.6.9` already ships; only power meters can use it. A generic `modbus_tcp` gateway type unlocks the largest device population in industry.
2. **No global profile library.** `METER_MODELS` is meter-shaped and meter-scoped.
3. **No CIP Parameter Object path.** Drives expose named parameters (PowerFlex "Output Freq" = parameter 1) via CIP object `0x0F`/`Get_Attribute_Single`, which is friendlier than slicing assembly bytes.

---

## 4. EtherCAT — recommended NOT to build, and why

EtherCAT is not "another Ethernet protocol you connect to". It is a Layer-2 protocol (EtherType `0x88A4`) with no IP, no TCP, and no sockets:

* the master must send raw Ethernet frames — on Windows that means **Npcap + admin**, a driver install on every edge box;
* it needs a **dedicated NIC**, because frames traverse the slave ring and come back; it cannot share the office LAN or a switch;
* it is cyclic and hard-real-time; Windows is not an RT OS, and this app is not an RT process;
* **a slave can have exactly one master.** Any EtherCAT device already wired to a PLC cannot accept us as a second master. Devices with no master at all are rare.

The same reasoning that ruled out PROFINET rules out EtherCAT, and more strongly. **How everyone else solves it:** read the data from the PLC that owns the segment (`siemens_snap7`, `siemens_opcua`, `allen_bradley` — all already supported), or put in an EtherCAT→Modbus TCP/OPC UA gateway. Both are already covered by the phases below.

If a hard requirement ever appears for a masterless EtherCAT segment, the honest route is `pysoem`/SOEM on a dedicated NIC, scoped as its own product decision — not as a driver alongside the others.

---

## 5. The plan

Deliberately staged. The July 2026 reliability crisis came from one day of concentrated change colliding with latent defects; every phase below ships and is gated on its own.

### Phase 1 — Generic Modbus TCP gateway *(highest value per unit of work)*
A `modbus_tcp` gateway type: host, port, unit id, and a table of registers (name, address, function, data type, word order, scale, offset, unit, tick). Reuses the block-merge read planner and per-address backoff already proven in `power_manager`. Register tables importable from a supplier CSV via the existing parser.
*Unlocks:* VSDs, energy meters, flow/temperature transmitters, weighing controllers, gateways of every other fieldbus.

### Phase 2 — The global device profile library
Move profiles out of `meter_registers` into `services/device_profiles.py`, keyed by `(manufacturer, model, protocol)` and carrying a tag template. Meter models migrate into it unchanged. One "Device catalogue" picker appears wherever a gateway is configured, for every protocol.

### Phase 3 — EtherNet/IP drive profiles: PowerFlex + Kinetix
No new driver. Add CIP **Parameter Object** reads (`0x0F` / `Get_Attribute_Single`) so a drive parameter can be addressed by number instead of by assembly offset, plus profiles for PowerFlex 525/753/755 and Kinetix 5500/5700 (speed, current, torque, DC bus, fault code, status word). EDS import already covers the assembly route for anything not in the catalogue.

### Phase 4 — One driver page for all of it
A single "Add device" flow: choose protocol → scan or type the address → pick a catalogue profile or import EDS/CSV → tick the values → save. The per-protocol mappers become panels inside it rather than separate pages.

### Out of scope, with reasons
EtherCAT and PROFINET masters (§4). Modbus **RTU** serial is a small follow-on to Phase 1 if a serial device ever appears — the register model is identical, only the transport differs.

---

## 6. Risks, and how each is contained

| Risk | Containment |
|---|---|
| A new protocol field is dropped by the UI Start payload | Already guarded: `test_gateway_ui_regressions.py` parses `GatewayConfig` and fails unless every field is sent or explained. This trap cost a full day on 2026-08-28. |
| More devices → more rows → the disk fills faster | Per-tag ticks ship with every profile; profiles default to a *useful subset*, never the whole address space. See the retention warning in `data-path-optimisation-2026-08-28.md` §9. |
| A slow or dead device stalls collection | The read-budget rule already applies: a driver that can overrun the loop's 8 s cap sits at RUNNING/W:0 with no error. Every new driver gets an explicit per-cycle deadline and a per-address backoff, as `power_manager` has. |
| Wrong register/offset returns a plausible number rather than an error | The "read live and show raw beside decoded" panel from the EtherNet/IP mapper is reused for Modbus before anything is saved. |
| Regression in what already works | Each phase is a new gateway type; existing types are untouched, and the release gate runs before each build. |
