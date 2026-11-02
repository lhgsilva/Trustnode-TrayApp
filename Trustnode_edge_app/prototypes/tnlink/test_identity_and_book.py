# -*- coding: utf-8 -*-
"""A unit keeps its id forever, and connections can be named and kept.

2026-10-31, requested: "every computer where it is installed keeps their own
id forever, and the connection id also the same so we can always reconnect,
and shows the recent connections and also save them a favorite and rename the
connection if needed".

Two things were in the way.

  * The unit id was tied to the HOSTNAME. Rename the PC - which customers do,
    and IT departments do in bulk - and the id was thrown away and re-minted.
    Every saved connection pointing at that machine went stale at the same
    moment. It is anchored to the Windows MachineGuid now, which survives a
    rename and an in-place upgrade, and which sysprep replaces when an image
    is cloned - the case the hostname check was really aiming at.

  * A connection had no lasting identity at all. Pairing codes are one-shot
    and rotate after every use, and that must stay that way - it is what
    stops a leaked code being replayed. So a saved connection is keyed by the
    other end's permanent UNIT ID, and the code is only ever the handshake.

    python prototypes/tnlink/test_identity_and_book.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="tnlink_id_")
os.environ["APPDATA"] = _TMP          # settings.json lands here

import tnlink                                                  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def settings():
    try:
        with open(tnlink._settings_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def main() -> int:
    print("LS-Link: a permanent unit id and a usable address book")
    # The peer must actually send its id, or none of this can be keyed.
    with open(os.path.join(HERE, "tnlink.py"), encoding="utf-8") as fh:
        src = fh.read()

    first = tnlink.unit_id()
    check("a unit mints an id", first.startswith("TNU-"), first)
    check("  and returns the same one next time", tnlink.unit_id() == first)

    # THE point: rename the machine.
    data = settings()
    data["unit_host"] = "some-old-name"
    with open(tnlink._settings_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    check("the id survives the computer being renamed",
          tnlink.unit_id() == first,
          "a rename used to mint a new id and orphan every saved connection")

    # A genuine clone - sysprep replaces the machine GUID - must NOT keep it.
    data = settings()
    data["unit_anchor"] = "00000000-0000-0000-0000-000000000000"
    with open(tnlink._settings_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    cloned = tnlink.unit_id()
    check("but a cloned image gets its own id", cloned != first,
          "two plants answering to one id is how traffic reaches the wrong site")

    # An id minted before this change is adopted, never re-minted.
    data = settings()
    data.pop("unit_anchor", None)
    data["unit_id"] = "TNU-OLD1-OLD2-OLD3"
    with open(tnlink._settings_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    check("an id from an older version is kept, not re-minted",
          tnlink.unit_id() == "TNU-OLD1-OLD2-OLD3",
          "an id the operator has written down must not change under them")
    check("  and is anchored from then on",
          bool(settings().get("unit_anchor")))

    # ---------------------------------------------------------- the book
    print()
    tnlink.remember_connection("tnu-aaaa-bbbb-cccc", host="PLANT-PC",
                               address="10.7.0.2", role="server")
    tnlink.remember_connection("TNU-DDDD-EEEE-FFFF", host="OFFICE-PC",
                               address="10.7.0.3")
    rows = tnlink.connections()
    check("a connection is remembered", len(rows) == 2, str(len(rows)))
    check("  keyed by the unit id, upper-cased",
          all(r["unit"] == r["unit"].upper() for r in rows))

    # Re-pairing must UPDATE the row, not add a second one.
    tnlink.remember_connection("TNU-AAAA-BBBB-CCCC", host="PLANT-PC",
                               address="10.7.0.9")
    rows = tnlink.connections()
    check("re-pairing updates the same entry", len(rows) == 2, str(len(rows)))
    hit = [r for r in rows if r["unit"] == "TNU-AAAA-BBBB-CCCC"][0]
    check("  and records the new address", hit.get("address") == "10.7.0.9")
    check("  and counts the uses", int(hit.get("uses") or 0) == 2, str(hit.get("uses")))

    check("renaming works", tnlink.rename_connection("TNU-AAAA-BBBB-CCCC", "Bay 6 rig"))
    hit = [r for r in tnlink.connections() if r["unit"] == "TNU-AAAA-BBBB-CCCC"][0]
    check("  the label is the operator's name", tnlink.connection_label(hit) == "Bay 6 rig")

    # A later connection must not overwrite the name the operator chose.
    tnlink.remember_connection("TNU-AAAA-BBBB-CCCC", host="PLANT-PC-RENAMED")
    hit = [r for r in tnlink.connections() if r["unit"] == "TNU-AAAA-BBBB-CCCC"][0]
    check("  and a later connection does not overwrite it",
          tnlink.connection_label(hit) == "Bay 6 rig",
          "re-pairing must not undo what the operator typed")

    check("favourites can be set", tnlink.favorite_connection("TNU-DDDD-EEEE-FFFF", True))
    check("  and sort to the top", tnlink.connections()[0]["unit"] == "TNU-DDDD-EEEE-FFFF")
    check("  and can be cleared", tnlink.favorite_connection("TNU-DDDD-EEEE-FFFF", False))

    # Unnamed rows are capped; favourites are never dropped.
    tnlink.favorite_connection("TNU-AAAA-BBBB-CCCC", True)
    for i in range(60):
        tnlink.remember_connection("TNU-%04d-0000-0000" % i, host="pc%d" % i)
    rows = tnlink.connections()
    check("the list stays readable", len(rows) <= 51, str(len(rows)))
    check("  and a favourite is never dropped",
          any(r["unit"] == "TNU-AAAA-BBBB-CCCC" for r in rows),
          "a machine the operator starred must still be there tomorrow")

    check("a connection can be forgotten",
          tnlink.forget_connection("TNU-AAAA-BBBB-CCCC")
          and not any(r["unit"] == "TNU-AAAA-BBBB-CCCC" for r in tnlink.connections()))

    check("the label falls back to the host, then the id",
          tnlink.connection_label({"unit": "TNU-1", "host": "PC"}) == "PC"
          and tnlink.connection_label({"unit": "TNU-1"}) == "TNU-1")

    check("the hello reply carries the unit id",
          '"unit": unit_id()' in src,
          "without it the other end has nothing permanent to file this under")

    # ------------------------------------------------ enter the code ONCE
    # 2026-10-31, requested: "everytime if I want to connect I can connect to
    # the same computer, without need to get new code all the time ... the
    # connection to the server id should be possible only entering once the
    # code".
    #
    # The code stays one-shot and still rotates. A successful code pairing
    # ALSO mints a long per-pair key: the server keeps its hash, the client
    # keeps the key, both filed under the other end's permanent unit id.
    print()
    ENG = "TNU-ENG1-ENG2-ENG3"
    check("an unknown engineer is not trusted", not tnlink.is_trusted(ENG))

    token = tnlink.grant_trust(ENG, host="PLANT-PC")
    check("pairing with the code mints a key", len(token) > 20)
    check("  the engineer is trusted now", tnlink.is_trusted(ENG))
    check("  and the key verifies", tnlink.trusted_secret(ENG, token))
    check("  but a wrong key does not", not tnlink.trusted_secret(ENG, token + "x"))
    check("  and the key itself is never stored in the clear",
          all("hash" in r and r.get("hash") != token
              for r in tnlink.trusted_units() if r["unit"] == ENG),
          "a stolen settings.json must not hand over working keys")

    # Revocation is what makes this safe to offer at all.
    check("the operator can revoke one engineer", tnlink.forget_trust(ENG))
    check("  who then cannot reconnect", not tnlink.trusted_secret(ENG, token))
    check("  and needs a fresh code", not tnlink.is_trusted(ENG))

    # Client side: the key is filed against the SERVER's unit id.
    tnlink.remember_connection("TNU-SRV1-SRV2-SRV3", host="PLANT-PC")
    check("the client keeps the key the server gave it",
          tnlink.remember_trust("TNU-SRV1-SRV2-SRV3", "a-very-long-test-key-value"))
    check("  and finds it again when reconnecting",
          tnlink.trust_token_for("TNU-SRV1-SRV2-SRV3") == "a-very-long-test-key-value")
    check("  with nothing for a server it has never met",
          tnlink.trust_token_for("TNU-NEVER-SEEN-0000") == "")

    # remember_trust needs the row to exist - this ordering dropped the key
    # on a FIRST pairing, which is the only pairing that hands one out.
    check("a key for an unknown row is not silently swallowed",
          tnlink.remember_trust("TNU-NO-SUCH-ROW0", "k") is False,
          "it must report failure, not pretend it stored something")

    # 2026-10-31. The pair-once feature was first built by sending a unit-id
    # preamble BEFORE the handshake so each end could choose a per-pair key.
    # It worked, and it broke interoperability: a new client could not pair
    # with a server still on the old build, so the engineer trying to reach a
    # customer saw the app die on connect - at exactly the moment they needed
    # it to diagnose that customer's edge.
    #
    # Reverted. The feature is still right; it has to be negotiated INSIDE
    # the existing version handshake, where an old peer can be detected and
    # fallen back to, rather than bolted on in front of it. These checks keep
    # the wire compatible until that is done.
    check("nothing is sent before the handshake",
          "_preamble_send" not in src.split("def handshake")[0].split("class ")[0]
          or "_preamble_send(s, unit_id())" not in src,
          "a new client must still be able to pair with an older server")
    check("  the handshake still authenticates with the code",
          "handshake(s, self.code, is_server=False)" in src
          and "handshake(sock, self.code, is_server=True)" in src)
    check("  and the server does not hand out a key nothing can use yet",
          '"trust": _grant' not in src)

    # ------------------------------------------- reconnect without a code
    # 2026-10-31, requested: "a way to reconnect to the same computer after
    # disconnect without re-entering the connection id code".
    #
    # The code is STILL burned after one session - a server forgotten on an
    # old machine must stop answering the code it gave out, which is why that
    # rule exists. What changed is who learns the next one: at pairing, over
    # the already-encrypted channel, the server tells the engineer it just
    # let in what its next code will be.
    print()
    SRV = "TNU-PLNT-0001-0001"
    check("nothing stored for a machine we have never paired with",
          tnlink.next_code_for(SRV) == "")

    check("the first pairing files the next code",
          tnlink.remember_next_code(SRV, "aaaa-bbbb-cccc-dddd"),
          "the FIRST pairing is the one that hands a code out - if the row "
          "has to exist first, the one that matters is dropped")
    check("  and it can be read back", tnlink.next_code_for(SRV) == "aaaa-bbbb-cccc-dddd")
    check("  against the unit id, so a rename does not lose it",
          any(r["unit"] == SRV for r in tnlink.connections()))

    check("a later pairing rolls it forward",
          tnlink.remember_next_code(SRV, "eeee-ffff-gggg-hhhh")
          and tnlink.next_code_for(SRV) == "eeee-ffff-gggg-hhhh")

    # Naming a machine must not disturb the code, and vice versa.
    tnlink.rename_connection(SRV, "Plant floor PC")
    check("renaming keeps the stored code",
          tnlink.next_code_for(SRV) == "eeee-ffff-gggg-hhhh")
    tnlink.remember_next_code(SRV, "iiii-jjjj-kkkk-llll")
    hit = [r for r in tnlink.connections() if r["unit"] == SRV][0]
    check("  and a new code keeps the name", tnlink.connection_label(hit) == "Plant floor PC")

    check("forgetting a machine forgets its code",
          tnlink.forget_connection(SRV) and tnlink.next_code_for(SRV) == "",
          "Forget must actually revoke, not just hide the row")

    # The wire and the server side.
    check("the server hands the next code to the engineer it let in",
          '"next_code": self.next_code' in src)
    check("  minted once per session, not on every hello",
          'if not getattr(self, "next_code", "")' in src,
          "a fresh code on each hello would leave the client holding a stale one")
    # 2026-11-02: the code is spent the moment it PAIRS (not when the session
    # ends), and the engineer who paired holds the next one - see _burn_code.
    check("  and the burned code is REPLACED by it, not by a random one",
          "self.code = self.next_code" in src and "def _burn_code" in src)
    check("  the one-shot rule is untouched",
          "spent = self.code" in src and "self._burn_code()" in src,
          "a forgotten server must still stop answering the code it gave out")
    check("the client stores what the server sent",
          "remember_next_code(str(self.info.get(" in src)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
