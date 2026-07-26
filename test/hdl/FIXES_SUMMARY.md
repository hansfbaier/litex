# litex keep_hierarchy clock aliasing — Fix Summary

## Applied Fixes

### Fix 1: `builder.py` — `--keep-hierarchy` implies `--hierarchical-verilog`

**File**: `litex/litex/soc/integration/builder.py` line 608
**Status**: ✅ Applied and verified

Before:
```python
if "hierarchical" not in kwargs:
    if self.hierarchical_keep_hierarchy:
        kwargs["hierarchical"] = {"enabled": True, "keep_hierarchy": True}
```
Problem: `hierarchical` is always in kwargs (default `False`).

After:
```python
if self.hierarchical_keep_hierarchy:
    kwargs["hierarchical"] = {"enabled": True, "keep_hierarchy": True}
elif "hierarchical" not in kwargs:
    kwargs["hierarchical"] = self.hierarchical
```

### Fix 2: `verilog.py` — Clock aliasing in `_emit()`

**File**: `litex/litex/gen/fhdl/verilog.py`
**Status**: ⚠️ Partially applied. Core aliasing logic works for priority 1+2.
              Priority 3 (renamed-domain fallback) is applied but causes
              Quartus errors (`value cannot be assigned to input`).

**What it does**:
1. After submodule instances, scans child external signals for clock/reset inputs.
2. Priority 1: parent domain by name → `assign child_clk = parent_clk;`
3. Priority 2: sibling daisy-chain → `assign child2_clk = child1_clk;`
4. Priority 3: fallback to any parent clock (handles renamed domains).

**What it doesn't yet handle**:
- When the parent module has `sys_clk` as an INPUT port, alias `assign write_clk = sys_clk`
  works but `assign sys_clk = sys_clk` (self-alias) causes Quartus error.
- `_absorb_child_clock_signals` tries to make child clock signals local wires
  but breaks sibling daisy-chaining (removes ports needed for aliasing).

## Test Results (latest)

```
test_keep_hierarchy_clock_aliasing.py::test_renamed_fifo_graycounter_clock_ports_not_floating — PASS ✅
test_keep_hierarchy_clock_aliasing.py::test_wrapper_top_daisychains_sibling_clocks — FAIL ❌
test_hierarchical_verilog.py (10 tests) — 10 PASS ✅
```

The failing test is the wrapper daisy-chain test (absorbing child signals broke it).

## Remaining Work

The `verilog.py` fix needs refinement:

1. `_absorb_child_clock_signals` must absorb signals WITHOUT removing them as child ports.
   The signal should be BOTH a local wire in the parent AND an external signal of the child.
   Currently it's either-or.

2. Self-aliases (`assign sys_clk = sys_clk`) need to be detected and suppressed.
   Name collision between parent's clock signal and child's clock port causes
   `ctx.ns.get_name` to return the same name for different Signal objects.

3. The priority 3 fallback always picks the first parent clock (`sys_clk`), but
   for `write→usb` / `read→sys` mapping, `write_clk` should go to `usb_clk` not `sys_clk`.
   Need a smarter heuristic or to preserve the `ClockDomainsRenamer` mapping.

## Build Status

The last DECA build attempt with all fixes applied:
```
Error (10231): value cannot be assigned to input "usb_clk" at terasic_deca.v(2002)
```
Self-aliases are generated in wrapper modules that lack their own clock domains.
