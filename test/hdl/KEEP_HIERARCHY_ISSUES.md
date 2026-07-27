# Hierarchical Verilog — Known Issues and Test Plan

## Current State

All known issues fixed. The ACM example builds with both `--hierarchical-verilog` and
`--hierarchical-verilog --keep-hierarchy` (Quartus OK, verified on DECA hardware).

Fixes applied:
- **`litex/litex/soc/integration/builder.py`**: `--keep-hierarchy` now implies `--hierarchical-verilog`.
- **`litex/litex/gen/fhdl/verilog.py`**: clock/reset aliasing added in `_emit()` (priorities 1 & 2 below).
- **`litex/litex/gen/fhdl/verilog.py`**: fallback alias (priority 3) no longer fires when the parent
  owns the domain — fixes the multiple-constant-driver bug (Issue 4).
- **`litex/litex/gen/fhdl/verilog.py`**: duplicate sibling submodule names disambiguated (Issue 5).
- **`litex/litex/gen/fhdl/verilog.py`**: renamed clock domains resolved via recovered
  `ClockDomainsRenamer` mappings; renamed-domain clocks stay local wires of the renamed module,
  aliased to the mapped parent domain (Issue 3).

---

## Issue 1: Builder ignores `--keep-hierarchy` without `--hierarchical-verilog`

✅ **FIXED**

**Before** (`builder.py`, old lines 608-611):
```python
if "hierarchical" not in kwargs:
    if self.hierarchical_keep_hierarchy:
        kwargs["hierarchical"] = {"enabled": True, "keep_hierarchy": True}
    else:
        kwargs["hierarchical"] = self.hierarchical
```

`"hierarchical"` is always in kwargs (set to `False` by `args.hierarchical_verilog`), so the guard
at line 608 is always false and `keep_hierarchy` never takes effect unless `--hierarchical-verilog`
is also passed.

**After**:
```python
if self.hierarchical_keep_hierarchy:
    kwargs["hierarchical"] = {"enabled": True, "keep_hierarchy": True}
elif "hierarchical" not in kwargs:
    kwargs["hierarchical"] = self.hierarchical
```

---

## Issue 2: Child submodule clock ports floating (no aliasing)

✅ **FIXED** for priority 1 (parent domain by name) and priority 2 (sibling daisy-chain).

**Before**: Child submodules get uniquely-named clock ports (e.g. `usb_clk_23`) that the parent
declares as wires but never drives. No `assign usb_clk_23 = usb_clk;` was ever generated.

**After** (`verilog.py`, `_emit()` at line 1440): A new block scans each child's `external_signals`
for clock/reset input ports and generates alias assignments driven by:

1. **Priority 1**: Parent domain by name match — if the parent fragment has a `ClockDomain` with
   the same name as the child's domain, alias `child_clk = parent_clk`.
2. **Priority 2**: Sibling daisy-chain — if parents lacks that domain, pick the first sibling's
   clock signal for that domain as canonical and alias all other children to it.

**Limitation**: Neither priority handles the case where the parent has the clock but under a
different domain name (see Issue 3).

---

## Issue 3: Renamed clock domains not aliased (GrayCounter inside `ClockDomainsRenamer`-wrapped AsyncFIFO)

✅ **FIXED** (2026-07-27)

### Where it happens

In `liteusb/liteusb/gateware/usb/devices/acm.py` (lines 280-282):
```python
self.submodules.rx_fifo = ClockDomainsRenamer(
    {"write": "usb", "read": "sys"})(AsyncFIFO(width=8, depth=128))
```

Inside `migen/migen/genlib/fifo.py` (lines 195-196):
```python
produce = ClockDomainsRenamer("write")(GrayCounter(depth_bits+1))
consume = ClockDomainsRenamer("read")(GrayCounter(depth_bits+1))
```

### Module hierarchy after renaming

```
usb_serial (parent, domains: sys/usb)
└── rx_fifo (AsyncFIFO, domains: sys/usb after outer renamer)
    ├── graycounter_0 (GrayCounter, domain: write — NOT renamed by outer renamer)
    └── graycounter_1 (GrayCounter, domain: read — NOT renamed by outer renamer)
```

The outer `ClockDomainsRenamer({"write": "usb", "read": "sys"})` renames `AsyncFIFO`'s own
fragment but does **not** recurse into the GrayCounter submodules' fragments.  The GrayCounters
still have `write`/`read` domain names while the parent `rx_fifo` has `usb`/`sys`.

### Generated Verilog (broken)

```verilog
module rx_fifo (
    input wire usb_clk_25,     // ← driven from above (usb_serial → rx_fifo)
    input wire sys_clk_1,      // ← driven from above
    input wire write_clk_1,    // ← FLOATING — never driven!
    input wire read_clk_1,     // ← FLOATING — never driven!
    input wire write_rst_1,    // ← FLOATING
    input wire read_rst_1,     // ← FLOATING
    ...
);
    graycounter_0 (                       // domain: "write"
        .write_clk_1(write_clk_1),        // ← undriven wire
        .write_rst_1(write_rst_1),        // ← undriven wire
    );
    graycounter_1 (                       // domain: "read"
        .read_clk_1(read_clk_1),          // ← undriven wire
        .read_rst_1(read_rst_1),          // ← undriven wire
    );
    // MISSING:
    //   assign write_clk_1 = usb_clk_25;
    //   assign write_rst_1 = usb_rst_25;
    //   assign read_clk_1  = sys_clk_1;
    //   assign read_rst_1  = sys_rst_1;
endmodule
```

### Why Priority 1 fails

The child graycounter has domain name `write`; the parent `rx_fifo` has `usb`/`sys`.
No name match → Priority 1 cannot find `source_sig`.

### Why Priority 2 fails

`canonical_clk["write"]` is set to `graycounter_0`'s own `cd.clk` — the same signal
as the port being aliased.  `source_sig is sig` → skipped.  The other graycounter's
`read` domain is similarly self-referential.

### Root cause

The mapping `write→usb, read→sys` is established by `ClockDomainsRenamer.transform_fragment()`
at Python elaboration time, but the mapping is **lost** after the fragment is transformed.
The hierarchical converter only sees the post-rename fragment with domain names `usb`/`sys`
and the GrayCounter fragment with `write`/`read`.  It cannot reconstruct the mapping.

### Fix (implemented)

Three parts, all in `litex/litex/gen/fhdl/verilog.py`:

1. **Recover the rename mapping.** `ClockDomainsRenamer` patches the module instance's
   `get_fragment` with a closure capturing the renamer, which stores the mapping as
   `cd_remapping`. `_collect_cd_remaps()` walks the closure chain (handles nested
   decorators) and each tree node gets a `remap_chain` (own + ancestors', nearest first).
2. **Remap-aware aliasing.** The alias block resolves a child's domain name through the
   node's `remap_chain` before the parent-domain lookup, so `write`→`usb`, `read`→`sys`
   produce `assign write_clk = usb_clk;` / `assign read_clk = sys_clk;` inside the FIFO.
3. **Virtual ownership at the renamer boundary.** Renamed-domain clock/reset signals
   (fresh domains created locally for renamed sync keys) are treated as owned by the
   nearest ancestor whose remap covers the domain name, so `write_clk`/`read_clk` become
   FIFO-local wires instead of input ports lifted up the hierarchy (assigning to an input
   port is illegal: Quartus "value cannot be assigned to input").

Additionally, global clock domains now win name collisions when merging into local
fragments (first-match, same as `_ClockDomainList` lookups in flat conversion), so
`ClockDomainsRenamer`-mutated "phantom" domains defer to the authoritative global domain.

Regression test: `test_hierarchical_renamed_domain_aliased_to_mapped_parent_domain` in
`test/hdl/test_hierarchical_verilog.py` (plus a structural invariant check: no module
assigns to its own input port anywhere in the output).

---

## Issue 4: Parent-owned clock/reset nets double-driven by fallback alias

✅ **FIXED** (2026-07-27)

**Symptom** (liteusb `acm_serial.py --hierarchical-verilog`, Quartus):
```
Error (10028): Can't resolve multiple constant drivers for net "usb_rst"
```

**Root cause**: When a child's clock/reset input port IS the parent's own net (shared global
`ClockDomain` object — the common case for SoC clocks like `usb`), priority 1 resolved
`source_sig is sig` → reset to `None` → priority 2 (canonical) hit the same object → `None`
→ priority 3 fallback picked the *first* parent domain (`sys`) and emitted
`assign usb_clk = sys_clk;` / `assign usb_rst = sys_rst;` while the top module's CRG comb
logic already drove those nets (`assign usb_clk = crg_max10pll1_clkout;`,
`assign usb_rst = (crg_por != 1'd0);`). Two siblings (two USBDevice subtrees) each emitted
the alias, making it three drivers.

**Fix** (`verilog.py`, `_emit()` clock/reset aliasing):
- Priority 1 self-match (`source_sig is sig`) now **skips** the port entirely — the port
  connection already ties the nets; any alias would be a second driver.
- Priority 2 self-match skips too when the parent has the domain by name; otherwise falls
  through to priority 3 (preserves the renamed-domain fallback behavior).
- Priority 3 fallback is now gated on `parent_cd is None` (parent genuinely lacks the domain).

**Regression tests** (`test/hdl/test_hierarchical_verilog.py`):
- `test_hierarchical_parent_driven_clock_not_double_driven` — CRG-style top driving
  `usb_clk`/`usb_rst`, child with `sync.usb`: no fallback alias, exactly one driver per net.
- `test_hierarchical_sibling_clocks_not_double_driven` — two siblings sharing the domain
  (mirrors the ACM two-USBDevice topology).

---

## Issue 5: Duplicate sibling submodule names collide as Verilog modules/instances

✅ **FIXED** (2026-07-27)

**Symptom** (`--keep-hierarchy`, Quartus):
```
Error (10228): module "...__USBStreamInEndpoint__tx_manager" cannot be declared more than once
Error (10149): identifier "USBStreamInEndpoint" is already declared in the present scope
```

**Root cause**: liteusb's `USBDevice` names endpoints by class name and tries to dedup with
`hasattr(self.submodules, name)` — but migen's `_ModuleSubmodules.__setattr__` only appends
to an internal list and never sets a real attribute, so the guard never fires and both
`USBStreamInEndpoint` instances are registered under the same name. Flat conversion tolerates
duplicate submodule names; the hierarchical module tree used them verbatim for path-derived
module names and instance names, producing duplicate declarations.

**Fix**: `_build_module_tree` now disambiguates duplicate sibling names (`endpoint`,
`endpoint_2`, ...).

**Regression test**: `test_hierarchical_duplicate_sibling_names_are_disambiguated`.

---

## Issue 6: Shared-alias descendant ids drop inlined logic

✅ **FIXED** (2026-07-27)

**Symptom** (DECA UAC2 audio interface, `--hierarchical-verilog`): device enumerates
(control endpoint + descriptors OK) but UAC2 class requests fail
(`parse_audio_format_rates_v2v3(): unable to retrieve number of sample rates`) and no
audio is produced.

**Root cause**: `UAC2RequestHandlers` is registered twice — under `USBControlEndpoint`
(via `add_request_handler`) and at SoC top level (`self.submodules.uac2_handlers`). The
second registration becomes a *shared alias*, which copies the owner's
`raw_desc_comb_ids`/`raw_desc_sync_ids`. When the whole chain (handler → control endpoint
→ USBDevice) is inlined into the top module, the handler's statements legitimately arrive
in the top's `inline_raw_*` via the inline chain — but `_lower_tree` subtracted the
alias's (non-inline) descendant ids, deleting the handler's `StreamSerializer`
(the response transmitter) from the netlist.

**Fix**: `_set_inline_children` excludes shared aliases from `filter_children` — aliases
are never emitted, so their ids must never filter anything.

**Regression test**: `test_hierarchical_shared_alias_does_not_drop_inlined_logic`.

---

## Issue 7: Clock aliases driven onto a module's own input ports

✅ **FIXED** (2026-07-27)

**Symptom** (DECA UAC2 audio interface, `--keep-hierarchy`, Quartus):
```
Error (10231): value cannot be assigned to input "sys_clk"
Error (10161): object "usb_rst" is not declared
```

**Root cause**: With the renamed-domain mapping (Issue 3) active, the alias block fired
at *every* level of a renamed subtree. Inside `audio_init` (wrapped in
`ClockDomainsRenamer("usb")`), the intermediate `init_streamer` module receives the
phantom `sys` clock/reset nets via its input ports — driven from above by the alias at
the renamer-boundary module. Emitting another alias at the intermediate level assigns to
an input port (illegal) and duplicates the driver.

**Fix**: `_generate_clock_reset_aliases` skips a child clock/reset port when the same
signal is itself an input port of the module being emitted. Aliases are only emitted
where the net is a local wire (the renamer-boundary module for renamed domains; the
parent that owns the clock otherwise).

**Regression test**: `test_hierarchical_renamed_subtree_aliases_only_at_boundary`
(plus a reusable structural invariant `_assert_no_input_port_drivers` checking that no
module in the whole output assigns to one of its own input ports).

---

## Test Plan

### Test 1: Clock aliasing — parent has matching domain (Priority 1)

A top module with `cd_usb` domain, a child submodule also using `cd_usb`.  Verify
that in keep-hierarchy output the child's `usb_clk` port is assigned to the top's
`usb_clk`:

```python
class _ClkParentTop(Module):
    def __init__(self):
        self.clock_domains.cd_usb = ClockDomain("usb")
        self.o = Signal()
        self.submodules.leaf = _ClkLeaf(sync_domain="usb")
        self.comb += self.o.eq(self.leaf.o)
```

**Expected**: `assign usb_clk = usb_clk;` (or equivalent alias) in the top module.

### Test 2: Clock aliasing — wrapper with no own domain (Priority 2, sibling daisy-chain)

A top module with NO clock domains, two sibling submodules that both use `sys` domain.
One child's `sys_clk` should become canonical; the other should alias to it.

```python
class _WrapperTop(Module):
    def __init__(self):
        self.o = Signal()
        self.submodules.child_a = _ClkLeaf(sync_domain="sys")
        self.submodules.child_b = _ClkLeaf(sync_domain="sys")
        self.comb += self.o.eq(child_a.o ^ child_b.o)
```

**Expected**: `assign sys_clk = sys_clk;` (daisy-chain) in the wrapper module.

### Test 3: Renamed domain — ClockDomainsRenamer (Issue 3)

A top module wrapping an `AsyncFIFO` (or equivalent) with `ClockDomainsRenamer`.
Verify that the GrayCounter clock ports are connected to the renamed parent clocks.

```python
class _RenamedFIFOTop(Module):
    def __init__(self):
        self.i = Signal(8)
        self.o = Signal(8)
        self.comb += self.i.eq(0xAA)
        self.submodules.fifo = ClockDomainsRenamer(
            {"write": "usb", "read": "sys"})(AsyncFIFO(width=8, depth=4))
```

**Expected**: Inside the `top__fifo` module, `assign write_clk = usb_clk;`
and `assign read_clk = sys_clk;` (or equivalent).

### Test 4: Multi-level clock propagation (SoC-like chain)

A three-level hierarchy: Grandparent → Parent → Child, where the grandparent has
`cd_usb`, parent is a wrapper (no domain), and child uses `usb`.  The clock should
propagate correctly through the wrapper.

### Test 5: Clock + Data signal combined

A more realistic scenario where a child has both a clock input AND a data signal
driven by the parent.  Ensure the clock gets aliased while the data signal is
connected via normal combinational logic, and neither breaks the other.

### Test 6: Reset signal aliasing

Same as Test 1 but for reset signals (`rst` instead of `clk`).

### Test 7: Binary equivalence — flat vs keep-hierarchy

For each test fixture, compare the set of unique signal names in flat output
vs keep-hierarchy output.  Every undriven wire in keep-hierarchy that is driven
in flat should have a corresponding alias in keep-hierarchy.
