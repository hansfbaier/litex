import unittest
import re

from migen import *
from migen.fhdl.decorators import ClockDomainsRenamer
from migen.fhdl.specials import Tristate
from migen.genlib.fifo import AsyncFIFO

from litex.gen import LiteXContext
from litex.gen.fhdl.hierarchy import LiteXHierarchyExplorer
from litex.gen.fhdl.verilog import convert


class _Leaf(Module):
    def __init__(self):
        # Intentionally unnamed submodule to exercise generated-name policy.
        self.submodules += Module()


class _Top(Module):
    def __init__(self):
        self.i = Signal()
        self.o = Signal()
        self.comb += self.o.eq(self.i)
        self.submodules.leaf = _Leaf()
        self.specials += Instance("MY_BB")


class _TristateLeaf(Module):
    def __init__(self, pad):
        self.o  = Signal(name="leaf_o")
        self.oe = Signal(name="leaf_oe")
        self.i  = Signal(name="leaf_i")

        self.comb += [
            self.o.eq(1),
            self.oe.eq(self.i),
        ]
        self.specials += Tristate(pad, self.o, self.oe, self.i)


class _TristateTop(Module):
    def __init__(self):
        self.pad = Signal(name="pad")
        self.submodules.leaf = _TristateLeaf(self.pad)


class _SharedLeaf(Module):
    def __init__(self, shared):
        self.o = Signal(name="leaf_o")
        self.comb += self.o.eq(shared)


class _SharedTop(Module):
    def __init__(self):
        self.dummy  = Signal(name="dummy")
        self.shared = Signal(name="shared")
        self.submodules.leaf = _SharedLeaf(self.shared)


class _InlineFSMLeaf(Module):
    def __init__(self):
        self.o = Signal(name="fsm_o")
        self.submodules.fsm = FSM(reset_state="IDLE")
        self.comb += self.o.eq(0)
        self.fsm.act("IDLE",
            NextState("RUN"),
        )
        self.fsm.act("RUN",
            self.o.eq(1),
            NextState("RUN"),
        )


class _InlineFSMTop(Module):
    def __init__(self):
        self.o = Signal(name="o")
        self.submodules.leaf = _InlineFSMLeaf()
        self.comb += self.o.eq(self.leaf.o)


class _ClockRenamedInlineFSMTop(Module):
    def __init__(self):
        self.clock_domains.cd_eth_tx = ClockDomain("eth_tx")
        self.o = Signal(name="o")
        self.submodules.leaf = ClockDomainsRenamer("eth_tx")(_InlineFSMLeaf())
        self.comb += self.o.eq(self.leaf.o)


class _MemoryLeaf(Module):
    def __init__(self):
        self.o = Signal(8, name="mem_o")
        mem = Memory(8, 4, init=[0x12, 0x34, 0x56, 0x78])
        port = mem.get_port()
        self.specials += mem
        self.comb += [
            port.adr.eq(0),
            self.o.eq(port.dat_r),
        ]


class _MemoryTop(Module):
    def __init__(self):
        self.o = Signal(8, name="o")
        self.submodules.leaf = _MemoryLeaf()
        self.comb += self.o.eq(self.leaf.o)


class _SharedMemoryOwner(Module):
    def __init__(self, mem):
        self.specials += mem


class _SharedMemoryReader(Module):
    def __init__(self, mem, adr):
        self.dat_r = Signal(8, name="shared_mem_dat_r")
        port = mem.get_port()
        self.specials += mem
        self.comb += [
            port.adr.eq(adr),
            self.dat_r.eq(port.dat_r),
        ]


class _SharedMemoryTop(Module):
    def __init__(self):
        self.adr = Signal(2, name="adr")
        self.o = Signal(8, name="o")
        mem = Memory(8, 4, init=[0x12, 0x34, 0x56, 0x78])
        self.submodules.owner = _SharedMemoryOwner(mem)
        self.submodules.reader = _SharedMemoryReader(mem, self.adr)
        self.comb += self.o.eq(self.reader.dat_r)


class _InlineDropLeaf(Module):
    """Grandchild with real logic and a boundary output."""
    def __init__(self):
        self.inp = Signal(name="inp")
        self.out = Signal(name="out")
        self.comb += self.out.eq(self.inp)


class _InlineDropChild(Module):
    def __init__(self):
        # Owned under this module's path; driven by the parent.
        self.trigger = Signal(name="trigger")
        self.submodules.leaf = _InlineDropLeaf()
        self.comb += self.leaf.inp.eq(self.trigger)


class _InlineDropTop(Module):
    def __init__(self):
        self.io = Signal(name="io")
        self.submodules.child = _InlineDropChild()
        self.comb += self.io.eq(self.child.leaf.out)
        # Parent drives a signal owned under the child's path, so the inline
        # policy inlines the child into the parent.
        self.comb += self.child.trigger.eq(1)


class _USBClockLeaf(Module):
    """Leaf using the 'usb' clock domain; defines no domain itself, so its
    fragment shares the parent's global 'usb' ClockDomain object."""
    def __init__(self):
        self.o = Signal(name="leaf_o")
        self.sync.usb += self.o.eq(~self.o)


class _TwoDomainTop(Module):
    """Top with a CRG-style 'usb' domain: the top module itself drives the
    usb clk/rst nets (from a PLL output / POR), and a child leaf uses
    sync.usb, exposing usb_clk/usb_rst as input ports tied to the very same
    top-level nets."""
    def __init__(self, n_leaves=1):
        # Domain order matters: 'sys' first, mirroring SoC clock domain
        # lists, so the alias fallback would pick sys_clk/sys_rst.
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clock_domains.cd_usb = ClockDomain("usb")
        self.por     = Signal(name="por")
        self.pll_out = Signal(name="pll_out")
        self.o       = Signal(name="o")
        # CRG-like: top drives its own usb clock/reset nets.
        self.comb += [
            self.cd_usb.clk.eq(self.pll_out),
            self.cd_usb.rst.eq(self.por),
        ]
        leaves = []
        for i in range(n_leaves):
            leaf = _USBClockLeaf()
            setattr(self.submodules, f"leaf{i}" if n_leaves > 1 else "leaf", leaf)
            leaves.append(leaf)
        terms = [l.o for l in leaves]
        x = terms[0]
        for t in terms[1:]:
            x = x ^ t
        self.comb += self.o.eq(x)


class _DupNamedLeaf(Module):
    def __init__(self):
        self.o = Signal(name="dup_o")
        self.sync += self.o.eq(~self.o)


class _DupNamedSiblingTop(Module):
    """Two children registered under the SAME submodule name (migen allows
    it: _ModuleSubmodules.__setattr__ appends to a list; liteusb's USBDevice
    does this for its two USBStreamInEndpoint instances). Each child has a
    leaf grandchild so that path-derived module names would collide too."""
    def __init__(self):
        self.o = Signal(name="o")
        for _ in range(2):
            child = Module()
            child.submodules.leaf = _DupNamedLeaf()
            setattr(self.submodules, "endpoint", child)
        self.comb += self.o.eq(0)


class _RenamedFIFOTop(Module):
    """AsyncFIFO wrapped in ClockDomainsRenamer: GrayCounters keep 'write'/
    'read' domains while the FIFO parent uses 'usb'/'sys'. Mirrors the
    liteusb ACM tx_fifo/rx_fifo structure."""
    def __init__(self):
        self.i = Signal(8, reset_less=True, name="i")
        self.o = Signal(8, name="o")
        self.submodules.fifo = ClockDomainsRenamer(
            {"write": "usb", "read": "sys"})(AsyncFIFO(width=8, depth=4))
        self.comb += [
            self.fifo.din.eq(self.i),
            self.fifo.we.eq(1),
            self.fifo.re.eq(1),
            self.o.eq(self.fifo.dout),
        ]


class _TwiceRegisteredSerializer(Module):
    """Leaf with real logic, mimics StreamSerializer wrapped in a renamer."""
    def __init__(self):
        self.i = Signal(8, name="ser_i")
        self.o = Signal(8, name="ser_o")
        self.sync += self.o.eq(self.i)


class _TwiceRegisteredHandler(Module):
    """Handler whose transmitter child is added in do_finalize (like
    liteusb's UAC2RequestHandlers)."""
    def __init__(self):
        self.claim = Signal(name="claim")

    def do_finalize(self):
        self.submodules.transmitter = ClockDomainsRenamer("usb")(
            _TwiceRegisteredSerializer())
        self.comb += self.claim.eq(self.transmitter.o[0])


class _TwiceRegisteredControlEp(Module):
    def __init__(self, handler):
        self.inner = Signal(name="ctrl_inner")
        self.submodules.H = handler
        # Parent drives the same signal as the child: policy-1 inlines H.
        self.comb += handler.claim.eq(1)


class _TwiceRegisteredTop(Module):
    """The same handler module registered under two parents (like liteusb's
    add_request_handler + self.submodules.uac2_handlers): the second
    registration becomes a shared alias. With the whole chain inlined into
    the top (policy 1 then policy 2), the alias's descendant statement ids
    must not filter out the handler logic arriving via the inline chain."""
    def __init__(self):
        self.o = Signal(name="o")
        handler = _TwiceRegisteredHandler()
        self.submodules.ctrl = _TwiceRegisteredControlEp(handler)
        self.submodules.uac2_handlers = handler
        # Drive a signal owned under ctrl: policy-2 inlines ctrl into top.
        self.comb += self.ctrl.inner.eq(1)
        self.comb += self.o.eq(0)


class TestHierarchicalVerilog(unittest.TestCase):
    @staticmethod
    def _module_body(verilog, name):
        match = re.search(rf"module {name} \(.*?endmodule", verilog, re.S)
        if match is None:
            raise AssertionError(f"module {name} not found")
        return match.group(0)

    def test_hierarchy_golden_text(self):
        expected = "\n".join([
            "_Top",
            "\u251c\u2500\u2500 leaf (_Leaf)",
            "\u2502    \u2514\u2500\u2500 module_0 (Module) [Gen]",
            "\u2514\u2500\u2500 [BB:MY_BB]",
            "Legend:",
            "  [Gen]: Auto-generated instance name.",
            "  [BB:NAME]: Blackbox instance (verilog Instance).",
            "",
        ])

        hierarchy = LiteXHierarchyExplorer(top=_Top(), with_colors=False).get_hierarchy()
        self.assertEqual(hierarchy, expected)

    def test_flat_and_hierarchical_smoke(self):
        flat_top = _Top()
        hier_top = _Top()
        flat_ios = {flat_top.i, flat_top.o}
        hier_ios = {hier_top.i, hier_top.o}

        old_top = LiteXContext.top
        try:
            LiteXContext.top = flat_top

            flat = convert(flat_top, ios=flat_ios, name="top", hierarchical=False).main_source
            LiteXContext.top = hier_top
            hier = convert(hier_top, ios=hier_ios, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        # Parity smoke: top-level interface and direct path are present in both.
        self.assertIn("module top (", flat)
        self.assertIn("module top (", hier)
        self.assertRegex(flat, r"input\s+wire\s+i")
        self.assertRegex(hier, r"input\s+wire\s+i")
        self.assertRegex(flat, r"output\s+wire\s+o")
        self.assertRegex(hier, r"output\s+wire\s+o")
        self.assertIn("assign o = i;", flat)
        self.assertIn("assign o = i;", hier)

        # Hierarchical mode should emit child module and submodule instantiation.
        self.assertIn("module top__leaf", hier)
        self.assertIn("top__leaf leaf", hier)

    def test_hierarchical_tristate_keeps_child_controls_local(self):
        top = _TristateTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.pad}, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        leaf_module = self._module_body(verilog, "top__leaf")
        top_module  = self._module_body(verilog, "top")

        self.assertRegex(leaf_module, r"inout\s+wire\s+pad")
        self.assertNotRegex(leaf_module.split(");", 1)[0], r"leaf_[ioe]")
        self.assertRegex(leaf_module, r"wire\s+leaf_i;")
        self.assertRegex(leaf_module, r"wire\s+leaf_o;")
        self.assertRegex(leaf_module, r"wire\s+leaf_oe;")
        self.assertIn("assign pad = leaf_oe ? leaf_o : 1'bz;", leaf_module)
        self.assertIn("assign leaf_i = pad;", leaf_module)

        self.assertIn(".pad(pad)", top_module)
        self.assertNotIn("leaf_i", top_module)
        self.assertNotIn("leaf_o", top_module)
        self.assertNotIn("leaf_oe", top_module)

    def test_hierarchical_parent_signal_used_by_child_is_port(self):
        leaf = _SharedTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = leaf
            verilog = convert(leaf, ios={leaf.dummy}, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        leaf_module = self._module_body(verilog, "top__leaf")
        top_module  = self._module_body(verilog, "top")

        self.assertRegex(leaf_module, r"input\s+wire\s+shared")
        self.assertIn(".shared(shared)", top_module)

    def test_hierarchical_inline_child_statements_are_not_duplicated(self):
        top = _InlineFSMTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.o}, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        leaf_module = self._module_body(verilog, "top__leaf")
        top_module  = self._module_body(verilog, "top")

        self.assertEqual(leaf_module.count("case (state)"), 1)
        self.assertEqual(leaf_module.count("always @(posedge sys_clk)"), 1)
        self.assertIn(".fsm_o(fsm_o)", top_module)
        self.assertNotIn(".state(state)", top_module)
        self.assertNotIn(".next_state(next_state)", top_module)

    def test_hierarchical_inline_child_preserves_renamed_clock_domain(self):
        top = _ClockRenamedInlineFSMTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.o}, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        leaf_module = self._module_body(verilog, "top__leaf")

        self.assertEqual(leaf_module.count("always @(posedge eth_tx_clk)"), 1)
        self.assertNotIn("sys_clk", leaf_module)
        self.assertNotIn("sys_rst", leaf_module)

    def test_hierarchical_memory_port_declares_clock(self):
        top = _MemoryTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.o}, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        leaf_module = self._module_body(verilog, "top__leaf")
        top_module  = self._module_body(verilog, "top")

        self.assertRegex(leaf_module, r"input\s+wire\s+sys_clk")
        self.assertIn("always @(posedge sys_clk)", leaf_module)
        self.assertIn(".sys_clk(sys_clk)", top_module)

    def test_hierarchical_inlined_child_keeps_grandchild_logic(self):
        # Regression test for the inline-flag reset bug: _mark_inline reset
        # every child's inline flag on each recursion, undoing
        # _inline_subtree()'s marks. A grandchild of an inlined child was
        # dropped from emission and its output became an undriven register.
        top = _InlineDropTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.io}, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        # The grandchild's driver must be emitted somewhere in the netlist;
        # buggy output left `out` as an undriven register instead.
        self.assertIn("assign out = inp", verilog)

    def test_hierarchical_keep_hierarchy_lifts_child_internal_drive_to_port(self):
        # With keep_hierarchy, a parent driving a child-internal signal must
        # NOT inline the child; the signal becomes a proper input port.
        top = _InlineDropTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.io}, name="top",
                hierarchical={"enabled": True, "keep_hierarchy": True}).main_source
        finally:
            LiteXContext.top = old_top

        # Child stays a module...
        child_module = self._module_body(verilog, "top__child")
        top_module   = self._module_body(verilog, "top")
        # ...with trigger as an input port...
        self.assertRegex(child_module, r"input\s+wire\s+trigger")
        # ...and the grandchild logic is still emitted.
        leaf_module = self._module_body(verilog, "top__child__leaf")
        self.assertIn("assign out = inp", leaf_module)
        self.assertIn(".trigger(trigger)", top_module)

    def test_hierarchical_shared_memory_is_emitted_once(self):
        top = _SharedMemoryTop()

        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.adr, top.o}, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

        owner_module = self._module_body(verilog, "top__owner")
        reader_module = self._module_body(verilog, "top__reader")

        self.assertEqual(verilog.count("reg [7:0] mem[0:3];"), 1)
        self.assertIn("reg [7:0] mem[0:3];", owner_module)
        self.assertNotIn("reg [7:0] mem[0:3];", reader_module)
        self.assertRegex(owner_module, r"output\s+wire\s+\[7:0\]\s+dat_r")
        self.assertRegex(reader_module, r"input\s+wire\s+\[7:0\]\s+dat_r")

    def _convert_hier(self, top, ios):
        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            return convert(top, ios=ios, name="top", hierarchical=True).main_source
        finally:
            LiteXContext.top = old_top

    def test_hierarchical_parent_driven_clock_not_double_driven(self):
        # Regression test for the multiple-constant-driver bug (Quartus:
        # "Can't resolve multiple constant drivers for net usb_rst").
        # The child's usb_clk/usb_rst input ports ARE the top module's own
        # nets (shared global ClockDomain object). The clock/reset aliasing
        # block must NOT emit a fallback alias (assign usb_clk = sys_clk):
        # the port connection already ties the nets, and an extra assign
        # creates a second, conflicting driver on a net the top module
        # already drives from its CRG logic.
        top = _TwoDomainTop()
        verilog = self._convert_hier(top, ios={top.por, top.pll_out, top.o})
        top_module = self._module_body(verilog, "top")
        leaf_module = self._module_body(verilog, "top__leaf")

        # No cross-domain fallback aliases.
        self.assertNotIn("assign usb_clk = sys_clk;", top_module)
        self.assertNotIn("assign usb_rst = sys_rst;", top_module)

        # Each clock/reset net has exactly one continuous-assign driver:
        # the CRG-like comb logic, nothing else.
        self.assertEqual(len(re.findall(r"assign usb_clk =", top_module)), 1)
        self.assertEqual(len(re.findall(r"assign usb_rst =", top_module)), 1)
        self.assertIn("assign usb_clk = pll_out;", top_module)
        self.assertIn("assign usb_rst = por;", top_module)

        # The child still gets proper input ports tied to the top nets.
        self.assertRegex(leaf_module, r"input\s+wire\s+usb_clk")
        self.assertRegex(leaf_module, r"input\s+wire\s+usb_rst")
        self.assertIn(".usb_clk(usb_clk)", top_module)
        self.assertIn(".usb_rst(usb_rst)", top_module)

    def test_hierarchical_sibling_clocks_not_double_driven(self):
        # Same as above but with two siblings sharing the 'usb' domain
        # (mirrors the liteusb ACM example, where two USBDevice subtrees
        # both sit under the top module): no fallback aliases, single
        # driver per clock/reset net.
        top = _TwoDomainTop(n_leaves=2)
        verilog = self._convert_hier(top, ios={top.por, top.pll_out, top.o})
        top_module = self._module_body(verilog, "top")

        self.assertNotIn("assign usb_clk = sys_clk;", top_module)
        self.assertNotIn("assign usb_rst = sys_rst;", top_module)
        self.assertEqual(len(re.findall(r"assign usb_clk =", top_module)), 1)
        self.assertEqual(len(re.findall(r"assign usb_rst =", top_module)), 1)
        self.assertEqual(top_module.count(".usb_clk(usb_clk)"), 2)
        self.assertEqual(top_module.count(".usb_rst(usb_rst)"), 2)

    def test_hierarchical_duplicate_sibling_names_are_disambiguated(self):
        # Regression test for duplicate module/instance declarations with
        # --keep-hierarchy: two siblings registered under the same submodule
        # name (liteusb USBDevice names both of its USBStreamInEndpoint
        # instances "USBStreamInEndpoint") produced two modules with the same
        # path-derived name and two instances with the same name in the
        # parent scope (Quartus: "module ... cannot be declared more than
        # once", "identifier ... is already declared in the present scope").
        top = _DupNamedSiblingTop()
        verilog = self._convert_hier(top, ios={top.o})

        # Every module declaration must be unique.
        module_names = re.findall(r"^module (\S+) \(", verilog, re.M)
        self.assertEqual(len(module_names), len(set(module_names)),
            f"duplicate module declarations: "
            f"{sorted(n for n in module_names if module_names.count(n) > 1)}")

        # Both siblings emitted, with disambiguated names.
        self.assertIn("module top__endpoint (", verilog)
        self.assertIn("module top__endpoint_2 (", verilog)
        self.assertIn("module top__endpoint__leaf (", verilog)
        self.assertIn("module top__endpoint_2__leaf (", verilog)

        # Instance names inside the parent must be unique too.
        top_module = self._module_body(verilog, "top")
        self.assertIn("top__endpoint endpoint (", top_module)
        self.assertIn("top__endpoint_2 endpoint_2 (", top_module)

    def test_hierarchical_renamed_domain_aliased_to_mapped_parent_domain(self):
        # Regression test for Issue 3 (renamed clock domains): the
        # GrayCounter 'write'/'read' clock ports inside a
        # ClockDomainsRenamer-wrapped AsyncFIFO must be aliased to the
        # mapped parent domains (write->usb, read->sys), not to an arbitrary
        # fallback domain, and the aliased nets must be FIFO-local wires,
        # not input ports of the FIFO module itself (assigning to an input
        # port is illegal: Quartus "value cannot be assigned to input").
        top = _RenamedFIFOTop()
        old_top = LiteXContext.top
        try:
            LiteXContext.top = top
            verilog = convert(top, ios={top.i, top.o}, name="top",
                hierarchical={"enabled": True, "keep_hierarchy": True}).main_source
        finally:
            LiteXContext.top = old_top
        fifo_module = self._module_body(verilog, "top__fifo")
        fifo_header  = fifo_module.split(");", 1)[0]

        # Aliases exist and use the MAPPED parent domains.
        self.assertIn("assign write_clk = usb_clk;", fifo_module)
        self.assertIn("assign write_rst = usb_rst;", fifo_module)
        self.assertIn("assign read_clk = sys_clk;",  fifo_module)
        self.assertIn("assign read_rst = sys_rst;",  fifo_module)

        # The aliased clock/reset nets are local wires, not input ports.
        self.assertNotRegex(fifo_header, r"input\s+wire\s+write_clk")
        self.assertNotRegex(fifo_header, r"input\s+wire\s+write_rst")
        self.assertNotRegex(fifo_header, r"input\s+wire\s+read_clk")
        self.assertNotRegex(fifo_header, r"input\s+wire\s+read_rst")
        self.assertRegex(fifo_module, r"wire\s+write_clk;")
        self.assertRegex(fifo_module, r"wire\s+read_clk;")

        # The GrayCounter instances still get their clock ports connected.
        self.assertIn(".write_clk(write_clk)", fifo_module)
        self.assertIn(".read_clk(read_clk)",   fifo_module)

        # Structural invariant: no module assigns to one of its own input
        # ports anywhere in the output.
        for mod in re.finditer(r"module (\S+) \((.*?)\);(.*?)endmodule",
                               verilog, re.S):
            header = mod.group(2)
            body   = mod.group(3)
            inputs = set(re.findall(r"input\s+wire\s+(?:\[[^\]]*\]\s*)?(\w+)",
                                    header))
            for assign in re.finditer(r"assign (\w+) =", body):
                self.assertNotIn(assign.group(1), inputs,
                    f"module {mod.group(1)} assigns to its own input port "
                    f"{assign.group(1)}")

    def test_hierarchical_shared_alias_does_not_drop_inlined_logic(self):
        # Regression test: a module registered under two parents (shared
        # alias) whose first registration sits inside a subtree that gets
        # inlined into the top. The alias is never emitted; its copied
        # descendant statement ids must not filter the handler logic that
        # legitimately arrives at the top via the inline chain. (Audio
        # interface bug: UAC2RequestHandlers' StreamSerializer was dropped,
        # the device enumerated but could not answer class requests.)
        top = _TwiceRegisteredTop()
        verilog = self._convert_hier(top, ios={top.o})

        # The serializer's logic must be emitted somewhere in the netlist.
        self.assertIn("ser_i", verilog)
        self.assertIn("ser_o", verilog)
        self.assertRegex(verilog, r"ser_o <= ser_i")
