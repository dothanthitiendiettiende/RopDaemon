#!/usr/bin/env python3

__author__ = "Pietro Borrello"
__copyright__ = "Copyright 2021, ROPD Project"
__license__ = "BSD 2-clause"
__email__ = "pietro.borrello95@gmail.com"

from binascii import unhexlify, hexlify
import random
from struct import pack, unpack
from itertools import permutations, combinations, groupby
from tqdm import *
from .Gadget import Gadget, Operations, Types
from .Gadget import *
from .RopChainKernel import RopChainKernel
from .RopChain import RopChain
from .GadgetBox import GadgetBox
from . import Arch
import networkx as nx
import lief
import sys
import logging

# TODO's:
# 1: distinguish between int 0x80 and syscall in Others
# 2: check memory offset accesses not to be outside writable interval

def select_best(gadget_list):
        """
        Selects the best gadget between semantically equivalent gadgets, based on dereferenced addresses, modified regs, and stack fix
        """
        best = None
        min_mod = None
        min_addr = None
        min_fix = None
        for g in gadget_list:
            if len(g.mem[0]) < min_addr or min_addr is None:
                best = g 
                min_mod = len(g.modified_regs)
                min_addr = len(g.mem[0])
                min_fix = g.stack_fix
            elif len(g.mem[0]) == min_addr:
                if len(g.modified_regs) < min_mod:
                    best = g
                    min_mod = len(g.modified_regs)
                    min_fix = g.stack_fix
                elif len(g.modified_regs) == min_mod:
                    if g.stack_fix < min_fix:
                        best = g 
                        min_fix = g.stack_fix
        return best


def gadget_quality(g):
        return ('unknown' in g.mem[0],
                len(g.mem[0]), len(g.modified_regs), g.stack_fix, (g.address_end - g.address))



class GadgetsCombiner(object):
    def __init__(self, filename, gadgets):
        self.filename =  filename
        self.gadgets = gadgets
        self.all_load_gadgets = []
        self.best_load_gadgets = []
        self.indipendent_load_gadgets = []
        self.load_kernels = {}
        self.write_kernel = RopChainKernel([])
        self.writable_interval =(None, None)
        self.kernels = []
        self.chain = None
        self.bin_sh_address = None
        self.writable_address = None
        
        # assuming all gadget of the same type
        if len(self.gadgets):
            Arch.init(self.gadgets[0].arch)

    def execve(self):
        self.find_writable_interval()
        self.setup_execve()

        self.find_load_gadgets()
        self.compute_load_kernels()
        # in unable to set some registers
        if set(self.register_values.keys()) - set([reg.name for reg in self.load_kernels]):
            print ('requested:', self.register_values.keys())
            raise Exception('Unable to set requested registers')

        self.compute_write_kernels(0x68732f6e69622f, self.bin_sh_address)

        syscall_gadget = (sorted(filter(lambda g: isinstance(g, Other_Gadget),  self.gadgets), key=gadget_quality) + [None])[0]

        self.compute_chain()

        if self.chain:
            self.chain.add(syscall_gadget)
            print (self.chain.dump())


    def stats(self):
        total = 0
        subtotals = {}
        # TODO: define gadget load analysis
        for g in self.gadgets:
            t = type(g)
            if t not in subtotals: subtotals[t] = 0
            total += 1
            subtotals[t] += 1

        print ("Found %d different gadgets" % total)
        for t in subtotals:
            print ('*', t.__name__, "%.2f" % (subtotals[t]/float(total) * 100) + '%')

    def setup_execve(self):
        self.bin_sh_address = self.writable_interval[1] - 8

        if Arch.ARCH_BITS == Arch.ARCH_64:
            self.register_values = {'rax': 0x3b, 'rdi': self.bin_sh_address, 'rsi': 0x0, 'rdx': 0x0}
        else:
            assert(Arch.ARCH_BITS == Arch.ARCH_32)
            self.register_values = {'eax': 0xb, 'ebx': self.bin_sh_address, 'ecx': 0x0, 'edx': 0x0}


    def find_writable_interval(self):
        binary = lief.parse(self.filename)
        max_size = 0
        for segment in binary.segments:
            if segment.virtual_address and segment.has(lief.ELF.SEGMENT_FLAGS.R) and segment.has(lief.ELF.SEGMENT_FLAGS.W):
                if segment.virtual_size > max_size:
                    max_size = segment.virtual_size
                    self.writable_interval = (
                        segment.virtual_address, segment.virtual_address + segment.virtual_size)
        if self.writable_interval == (None, None):
            raise Exception('Writable Address not found')

        self.writable_address = (
            self.writable_interval[1] + self.writable_interval[0]) // 2

    @staticmethod
    def is_safely_inside(interval, address):
        pass

    @staticmethod
    def safe_value(interval, address, offset_of_address):
        pass


    def compute_load_kernels(self):
        kernels = {}
        for reg in self.indipendent_load_gadgets:
            kernels[reg] = RopChainKernel([GadgetBox(self.indipendent_load_gadgets[reg], value=self.register_values.get(reg.name, None))])
        
        missing_regs = [
            reg for reg in Arch.Registers if reg not in kernels.keys()]

        found_one = True
        while found_one:
            found_one = False
            for reg in missing_regs:
                best_guess = (sorted(filter(lambda g: set(g.mem[0]).issubset(
                    set(kernels.keys())), 
                    self.all_load_gadgets[reg]), key=gadget_quality)+[None])[0]

                if best_guess: 
                    if len(best_guess.mem[0])==1:
                        k = kernels[list(best_guess.mem[0])[0]].copy()
                        k.gadget_boxes[-1].value = self.writable_address
                        k.add(best_guess, value=self.register_values.get(reg.name, None))
                        kernels[reg] = k

                        found_one = True
                    elif len(best_guess.mem[0])==0:
                        kernels[reg] = RopChainKernel([GadgetBox(
                            best_guess, value=self.register_values.get(reg.name, None))])


            missing_regs = [
                reg for reg in Arch.Registers if reg not in kernels.keys()]

        self.kernels += [kernels[reg]
                         for reg in kernels if reg.name in self.register_values]
        kernels.values()
        self.load_kernels = kernels

        print ('[+] found best guesses for:', [reg.name for reg in self.load_kernels])

    def compute_write_kernels(self, what, where):
        best_write_gadget = (sorted(filter(lambda g: isinstance(g, WriteMem_Gadget) and set([g.addr_reg, g.src]).issubset(
            set(self.load_kernels.keys())),
            self.gadgets), key=gadget_quality) + [None])[0]

        assert(isinstance(what, int))
        assert(where < Arch.MAX_INT)
        if (what > Arch.MAX_INT):
            to_write = [(what & Arch.MAX_INT, where), (what >> Arch.ARCH_BITS, where + (Arch.ARCH_BITS // 8))]
        else:
            to_write = [(what, where)]

        for what, where in to_write:
            if best_write_gadget:
                if len(best_write_gadget.mem[0]) == 1:
                    k1 = self.load_kernels[best_write_gadget.addr_reg].copy()
                    k1.gadget_boxes[-1].value = (where -
                                                k1.gadget_boxes[-1].gadget.offset) & Arch.MAX_INT

                    k2 = self.load_kernels[best_write_gadget.src].copy()
                    # hex(pwn.u64('/bin/sh\x00'))
                    k2.gadget_boxes[-1].value = what

                    k = RopChainKernel([GadgetBox(
                        best_write_gadget, value=None)])

                    chain = RopChain([k1,k2,k])
                    
                    tmp_values = {best_write_gadget.addr_reg.name: where,
                                best_write_gadget.src.name: what}
                    
                    if all(value != tmp_values[reg] for (reg, value) in chain.evaluate().items() if reg in tmp_values):
                        chain = RopChain([k2, k1, k])
                    
                    # re-evaluate with new chain
                    if all(value != tmp_values[reg] for (reg, value) in chain.evaluate().items() if reg in tmp_values):
                        print ('[-] Unable to find write memory gadget, setting registers anyway')
                        return

                    # update write_kernel
                    self.write_kernel = RopChainKernel(self.write_kernel.gadget_boxes + chain.gadget_boxes)
                else:
                    print ('[-] [TODO] Unable to find a simple write memory gadget, setting registers anyway')
                    return
            else:
                print ('[-] Unable to find write memory gadget, setting registers anyway')
                return

        self.kernels.append(self.write_kernel)

    def compute_chain(self):
        print ('[+] computing sequence')
        kernels_list = [kernel for (
            reg, kernel) in self.load_kernels.items() if reg.name in self.register_values]
        
        kernel_graph = nx.DiGraph()
        kernel_graph.add_nodes_from([kernel.dest() for kernel in kernels_list])
        for kernel in kernels_list:
            kernel_graph.add_edges_from([(kernel.dest(), mod_reg) for mod_reg in kernel.modified_regs if kernel.dest() != mod_reg])
        try:
            registers_order = list(nx.topological_sort(kernel_graph))
            kernels_list = [self.load_kernels[reg] for reg in registers_order]
        except nx.exception.NetworkXUnfeasible:
            raise Exception('Unable to combine found gadgets')

        chain = RopChain([self.write_kernel]+kernels_list)
        chain.simplify()

        bad = False
        _register_values = chain.evaluate()
        for reg in _register_values:
            if(reg in self.register_values and _register_values[reg] != self.register_values[reg]):
                bad = True
        if bad:
            raise Exception('AAAAAAAAAAAAAH! The generated chain does not correctly set registers')
        self.chain = chain
        return

    def find_load_gadgets(self):

        all_load_gadgets = {reg: sorted(filter(lambda x: isinstance(x, LoadConst_Gadget) and x.dest is reg, self.gadgets), key=gadget_quality ) for reg in Arch.Registers}
        best_load_gadgets = {reg : (all_load_gadgets[reg]+[None])[0] for reg in all_load_gadgets}
    
        indipendent_regs = {}
        for (r, g) in best_load_gadgets.items():
            if g is not None and len(g.modified_regs) == 1 and len(g.mem[0]) == 0:
                indipendent_regs[r] = g

        self.all_load_gadgets = all_load_gadgets
        self.best_load_gadgets = best_load_gadgets
        self.indipendent_load_gadgets = indipendent_regs





