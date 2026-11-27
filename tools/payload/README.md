# payload/ud_wbi_drop.elf

Sony's own WBI invalidation utility, extracted from the official ZV-E10 2.03
update package:

| Property | Value |
|---|---|
| bytes | 6200 |
| SHA-256 | `f2a5b27a0c11d55a612d61e61e82c55278efb744052ecb8ec2cbbd8851d0ea6e` |
| toolchain | `GCC: (20181205 (Sony CE Linux 8.2.6.0))` |
| behaviour | parses `partinf.conf`, finds the `type=2` row, opens `/dev/nflasha5`, zeroes the first 512 bytes, prints `[UD_WBI_DROP] <dev> wbi_drop` |

`tools/wbi_coldboot.py` pins the size and hash and refuses to run anything else.
It never ships a generic block writer: recovery is the official updater, which
rewrites the whole `nflasha5` payload.

The same hash is independently pinned by `ZV-E10-5GHz-WiFi-Unlock`
(`tools/wbi.py: TOOL_HASH`), which is where this verification method comes from.