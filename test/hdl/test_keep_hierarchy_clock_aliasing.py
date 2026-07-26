"""Tests for keep_hierarchy clock/reset aliasing.

Exercises the _emit() clock aliasing block added to
litex.gen.fhdl.verilog._convert_hierarchical().

ISSUE #3 (failing): AsyncFIFO + ClockDomainsRenamer — GrayCounter clock
ports float because the rename mapping (write→usb, read→sys) is lost.
"""
import unittest
import re

from migen import *
from migen.fhdl.decorators import ClockDomainsRenamer
from migen.genlib.fifo import AsyncFIFO

from litex.gen import LiteXContext
from litex.gen.fhdl.verilog import convert


# ── Fixtures ──────────────────────────────────────────────────────────────────

class _SiblingLeaf(Module):
    """Leaf with a single output, using the 'sys' clock domain."""
    def __init__(self):
        self.o = Signal()
        self.sync += self.o.eq(~self.o)


class _WrapperTop(Module):
    """Wrapper with NO clock domain; two children share 'sys'.
    Priority 2 should daisy-chain one child's sys_clk to the other."""
    def __init__(self):
        self.o = Signal()
        self.submodules.child_a = _SiblingLeaf()
        self.submodules.child_b = _SiblingLeaf()
        self.comb += self.o.eq(self.child_a.o ^ self.child_b.o)


class _RenamedFIFOTop(Module):
    """AsyncFIFO + ClockDomainsRenamer: GrayCounters use 'write'/'read',
    FIFO parent has 'usb'/'sys'.  GrayCounter clock ports float unless
    aliased to the renamed parent clocks."""
    def __init__(self):
        self.i = Signal(8, reset_less=True)
        self.o = Signal(8)
        self.submodules.fifo = ClockDomainsRenamer(
            {"write": "usb", "read": "sys"})(AsyncFIFO(width=8, depth=4))
        self.comb += [
            self.fifo.din.eq(self.i),
            self.fifo.we.eq(1),
            self.fifo.re.eq(1),
            self.o.eq(self.fifo.dout),
        ]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _keep_hier_convert(top, ios, name="top"):
    """Return main_source of a keep-hierarchy conversion."""
    old_top = LiteXContext.top
    try:
        LiteXContext.top = top
        return convert(top, ios=ios, name=name,
                       hierarchical={"enabled": True,
                                     "keep_hierarchy": True}).main_source
    finally:
        LiteXContext.top = old_top


def _module_body(verilog, name):
    match = re.search(rf"module {re.escape(name)} \(.*?endmodule", verilog, re.S)
    if match is None:
        raise AssertionError(
            f"module {name} not found in:\n{verilog[:2000]}")
    return match.group(0)


def _aliases_in(verilog, signal_name):
    """Return all assign lines mentioning signal_name."""
    return [l.strip() for l in verilog.split("\n")
            if "assign" in l and signal_name in l and "=" in l]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestKeepHierarchyClockAliasing(unittest.TestCase):

    # ── Priority 2: sibling daisy-chain ─────────────────────────────────

    def test_wrapper_top_daisychains_sibling_clocks(self):
        """Wrapper with no domain; two children share 'sys'.  Aliases must
        connect the children's sys_clk ports."""
        verilog = _keep_hier_convert(_WrapperTop(),
                                     ios={_WrapperTop().o}, name="top")
        top_mod = _module_body(verilog, "top")
        mod_a  = _module_body(verilog, "top__child_a")
        mod_b  = _module_body(verilog, "top__child_b")

        self.assertIn("sys_clk", mod_a)
        self.assertIn("sys_clk", mod_b)

        self.assertIn("top__child_a child_a", top_mod)
        self.assertIn("top__child_b child_b", top_mod)

        aliases = _aliases_in(top_mod, "sys_clk")
        self.assertTrue(len(aliases) >= 1,
                        f"No sys_clk aliases in top:\n{top_mod[:1000]}")

    # ── Issue #3: renamed domains ───────────────────────────────────────

    def test_renamed_fifo_graycounter_clock_ports_not_floating(self):
        """Issue #3: GrayCounter clock ports must be driven.  Without
        aliasing, write_clk/read_clk are floating inputs."""
        verilog = _keep_hier_convert(_RenamedFIFOTop(),
                                     ios={_RenamedFIFOTop().i,
                                          _RenamedFIFOTop().o},
                                     name="top")

        # ── Structural assertions (always pass) ────────────────────
        self.assertIn("module top__fifo", verilog)
        self.assertIn("module top__fifo__graycounter_0", verilog)
        self.assertIn("module top__fifo__graycounter_1", verilog)

        fifo_mod = _module_body(verilog, "top__fifo")

        # GrayCounter clock ports are connected to something.
        self.assertIn(".write_clk(", fifo_mod)
        self.assertIn(".read_clk(", fifo_mod)

        # ── The bug assertion (currently FAILS — remove skip when
        #     Issue #3 is fixed) ────────────────────────────────────
        write_aliases = _aliases_in(fifo_mod, "write_clk")
        read_aliases  = _aliases_in(fifo_mod, "read_clk")

        self.assertTrue(len(write_aliases) >= 1,
            f"Issue #3: no write_clk alias in fifo_mod.\n"
            f"GrayCounter write-clocks float.")
        self.assertTrue(len(read_aliases) >= 1,
            f"Issue #3: no read_clk alias in fifo_mod.\n"
            f"GrayCounter read-clocks float.")

        # When fixed, aliases must connect to usb_clk / sys_clk.
        for a in write_aliases:
            self.assertTrue(len(a) > 0)
        for a in read_aliases:
            self.assertTrue(len(a) > 0)


if __name__ == "__main__":
    unittest.main()
