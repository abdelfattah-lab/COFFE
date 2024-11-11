# This module contains classes that describe FPGA circuitry. 
#
# The most important class (and the only one that should be instantiated outside this 
# file) is the 'fpga' class defined at the bottom of the file. 
#
# An 'fpga' object describes the FPGA that we want to design. A tile-based FPGA is 
# assumed, which consists of a switch block, a connection block and a logic cluster.
# Likewise, the 'fpga' object contains a 'SwitchBlockMUX' object, a 'ConnectionBlockMUX' 
# and a 'LogicCluster' object, each of which describe those parts of the FPGA in more detail.
# The 'LogicCluster' contains other objects that describe the various parts of its circuitry
# (local routing multiplexers, LUTs, FF, etc.) When you create an 'fpga' object, you 
# specify architecture parameters along with a few process parameters which are stored 
# in the 'fpga' object. 
#
# The 'fpga' does more than just hold information about the FPGA.
#
#      - It uses this information to generate the SPICE netlists that COFFE uses to 
#        measure delay. These netlists are generated with the appropriate transistor and
#        wire loading, which are a function of architecture parameters, transistor sizes as
#        well as some hard-coded layout assumptions (see [1] for layout assumption details).
#        It is important to note that these netlists only contain 'transistor-size' and 
#        'wire-load' VARIABLES and not hard-coded sizes and wire loads. These variables are
#        defined in their own external files. This allows us to create a single set of netlists. 
#        As COFFE changes the sizes of transistors, it only has to modify these external
#        files and the netlist will behave appropriately (transistor and wire loads depend on 
#        transistor sizes). 
#
#      - It can calculate the physical area of each circuit and structure inside the FPGA 
#        (transistors, MUXes, LUTs, BLE, Logic Cluster, FPGA tile, etc.) based on the sizes of
#        transistors and circuit topologies.
#
#      - It can calculate the length of wires in the FPGA circuitry based on the area of 
#        the circuitry and the layout assumptions.
#
#      - It can report the delay of each subcircuit in the FPGA.
#
# COFFE's transistor sizing engine uses the 'fpga' object to evaluate the impact of different transistor
# sizing combinations on the area and delay of the FPGA.
#
# [1] C. Chiasson and V. Betz, "COFFE: Fully-Automated Transistor Sizing for FPGAs", FPT2013

import os
import sys
import math

# Subcircuit Modules
import basic_subcircuits
import mux_subcircuits
import lut_subcircuits
import ff_subcircuits
import load_subcircuits
import memory_subcircuits
import utils
import hardblock_functions
import tran_sizing
import collections

# Top level file generation module
import top_level

# HSPICE handling module
import spice

# Track-access locality constants
OUTPUT_TRACK_ACCESS_SPAN = 0.25
INPUT_TRACK_ACCESS_SPAN = 0.50

# Delay weight constants:
DELAY_WEIGHT_SB_MUX = 0.4107
DELAY_WEIGHT_CB_MUX = 0.0989
DELAY_WEIGHT_LOCAL_MUX = 0.0736
DELAY_WEIGHT_LUT_A = 0.0396
DELAY_WEIGHT_LUT_B = 0.0379
DELAY_WEIGHT_LUT_C = 0.0704 # This one is higher because we had register-feedback coming into this mux.
DELAY_WEIGHT_LUT_D = 0.0202
DELAY_WEIGHT_LUT_E = 0.0121
DELAY_WEIGHT_LUT_F = 0.0186
DELAY_WEIGHT_LUT_FRAC = 0.0186
DELAY_WEIGHT_LOCAL_BLE_OUTPUT = 0.0267
DELAY_WEIGHT_GENERAL_BLE_OUTPUT = 0.0326
DELAY_WEIGHT_FF = 0.0326
# The res of the ~15% came from memory, DSP, IO and FF based on my delay profiling experiments.
DELAY_WEIGHT_RAM = 0.15
HEIGHT_SPAN = 0.5

DELAY_WEIGHT_LUT_INPUTS = (DELAY_WEIGHT_LUT_A, DELAY_WEIGHT_LUT_B, DELAY_WEIGHT_LUT_C, 
                           DELAY_WEIGHT_LUT_D, DELAY_WEIGHT_LUT_E, DELAY_WEIGHT_LUT_F)

# This parameter determines if RAM core uses the low power transistor technology
# It is strongly suggested to keep it this way since our
# core RAM modules were designed to operate with low power transistors.
# Therefore, changing it might require other code changes.
# I have included placeholder functions in case someone really insists to remove it
# The easier alternative to removing it is to just provide two types of transistors which are actually the same
# In that case the user doesn't need to commit any code changes.
use_lp_transistor = 1

class _Specs:
    """ General FPGA specs. """
 
    def __init__(self, arch_params_dict, quick_mode_threshold):
        
        # FPGA architecture specs
        self.N                       = arch_params_dict['N']
        self.K                       = arch_params_dict['K']
        self.W                       = arch_params_dict['W']
        self.L                       = arch_params_dict['L']
        self.I                       = arch_params_dict['I']
        self.Fs                      = arch_params_dict['Fs']
        self.Fcin                    = arch_params_dict['Fcin']
        self.Fcout                   = arch_params_dict['Fcout']
        self.Fclocal                 = arch_params_dict['Fclocal']
        self.num_ble_general_outputs = arch_params_dict['Or']
        self.num_ble_local_outputs   = arch_params_dict['Ofb']
        self.num_cluster_outputs     = self.N*self.num_ble_general_outputs
        self.Rsel                    = arch_params_dict['Rsel']
        self.Rfb                     = arch_params_dict['Rfb']
        self.use_fluts               = arch_params_dict['use_fluts']
        self.independent_inputs      = arch_params_dict['independent_inputs']
        self.enable_carry_chain      = arch_params_dict['enable_carry_chain']
        self.carry_chain_type        = arch_params_dict['carry_chain_type']
        self.FAs_per_flut            = arch_params_dict['FAs_per_flut']


        # BRAM specs
        self.row_decoder_bits     = arch_params_dict['row_decoder_bits']
        self.col_decoder_bits     = arch_params_dict['col_decoder_bits']
        self.conf_decoder_bits    = arch_params_dict['conf_decoder_bits']
        self.sense_dv             = arch_params_dict['sense_dv']
        self.worst_read_current   = arch_params_dict['worst_read_current']
        self.quick_mode_threshold = quick_mode_threshold
        self.vdd_low_power        = arch_params_dict['vdd_low_power']
        self.vref                 = arch_params_dict['vref']
        self.number_of_banks      = arch_params_dict['number_of_banks']
        self.memory_technology    = arch_params_dict['memory_technology']
        self.SRAM_nominal_current = arch_params_dict['SRAM_nominal_current']
        self.MTJ_Rlow_nominal     = arch_params_dict['MTJ_Rlow_nominal']
        self.MTJ_Rlow_worstcase   = arch_params_dict['MTJ_Rlow_worstcase']
        self.MTJ_Rhigh_worstcase  = arch_params_dict['MTJ_Rhigh_worstcase']
        self.MTJ_Rhigh_nominal    = arch_params_dict['MTJ_Rhigh_nominal']
        self.vclmp                = arch_params_dict['vclmp']
        self.read_to_write_ratio  = arch_params_dict['read_to_write_ratio']
        self.enable_bram_block    = arch_params_dict['enable_bram_module']
        self.ram_local_mux_size   = arch_params_dict['ram_local_mux_size']


        # Technology specs
        self.vdd                    = arch_params_dict['vdd']
        self.vsram                  = arch_params_dict['vsram']
        self.vsram_n                = arch_params_dict['vsram_n']
        self.gate_length            = arch_params_dict['gate_length']
        self.min_tran_width         = arch_params_dict['min_tran_width']
        self.min_width_tran_area    = arch_params_dict['min_width_tran_area']
        self.sram_cell_area         = arch_params_dict['sram_cell_area']
        self.trans_diffusion_length = arch_params_dict['trans_diffusion_length']
        self.metal_stack            = arch_params_dict['metal']
        self.model_path             = arch_params_dict['model_path']
        self.model_library          = arch_params_dict['model_library']
        self.rest_length_factor     = arch_params_dict['rest_length_factor']
        self.hb_files               = arch_params_dict['hb_files']
        self.use_tgate              = arch_params_dict['use_tgate']
        self.use_finfet             = arch_params_dict['use_finfet']

        self.updates                = arch_params_dict['updates']
        self.mult_size              = arch_params_dict['mult_size']

        
        
class _SizableCircuit:
    """ This is a base class used to identify FPGA circuits that can be sized (e.g. transistor sizing on lut)
        and declare attributes common to all SizableCircuits.
        If a class inherits _SizableCircuit, it should override all methods (error is raised otherwise). """
        
    def __init__(self, name, use_tgate = False, use_finfet = False):    
        
        # Component name
        self.name = name
        # A list of the names of transistors in this subcircuit. This list should be logically sorted such 
        # that transistor names appear in the order that they should be sized
        self.transistor_names = []
        # A list of the names of wires in this subcircuit
        self.wire_names = []
        # A dictionary of the initial transistor sizes
        self.initial_transistor_sizes = {}
        # Path to the top level spice file
        self.top_spice_path = ""    
        # Fall time for this subcircuit
        self.tfall = 1
        # Rise time for this subcircuit
        self.trise = 1
        # Delay to be used for this subcircuit
        self.delay = 1
        # Delay weight used to calculate delay of representative critical path
        self.delay_weight = 1
        # Dynamic power for this subcircuit
        self.power = 1
        # Boolean for using tranmission gate switches
        self.use_tgate = use_tgate
        # Boolean for using finfet transistors
        self.use_finfet = use_finfet

    # TODO: call the constructor of this base calss from all the child classes and remove those class
    # variables there is no need to have them as class variables at all

    # A list of the names of transistors in this subcircuit. This list should be logically sorted such 
    # that transistor names appear in the order that they should be sized.
    transistor_names = []
    # A list of the names of wires in this subcircuit
    wire_names = []
    # A dictionary of the initial transistor sizes
    initial_transistor_sizes = {}
    # Path to the top level spice file
    top_spice_path = ""    
    # Fall time for this subcircuit
    tfall = 1
    # Rise time for this subcircuit
    trise = 1
    # Delay to be used for this subcircuit
    delay = 1
    # Delay weight used to calculate delay of representative critical path
    delay_weight = 1
    # Dynamic power for this subcircuit
    power = 1

    
    def generate(self):
        """ Generate SPICE subcircuits.
            Generate method for base class must be overridden by child. """
        msg = "Function 'generate' must be overridden in class _SizableCircuit."
        raise NotImplementedError(msg)
       
       
    def generate_top(self):
        """ Generate top-level SPICE circuit.
            Generate method for base class must be overridden by child. """
        msg = "Function 'generate_top' must be overridden in class _SizableCircuit."
        raise NotImplementedError(msg)
     
     
    def update_area(self, area_dict, width_dict):
        """ Calculate area of circuit.
            Update area method for base class must be overridden by child. """
        msg = "Function 'update_area' must be overridden in class _SizableCircuit."
        raise NotImplementedError(msg)
        
        
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        msg = "Function 'update_wires' must be overridden in class _SizableCircuit."
        raise NotImplementedError(msg)
                
  
class _CompoundCircuit:
    """ This is a base class used to identify FPGA circuits that should not be sized. These circuits are
        usually composed of multiple smaller circuits, so we call them 'compound' circuits.
        Examples: circuits representing routing wires and loads. 
        If a class inherits _CompoundCircuit, it should override all methods."""
    def __init__(self, name, use_tgate = False, use_finfet = False):
        # Compound Circuit's name
        self.name = name
        # Boolean for using tgate switches
        self.use_tgate = use_tgate
        # Boolean for using finfet transistors
        self.use_finfet = use_finfet
        # A Subcircuits dictionary holding all the subcircuits in this compound circuit
        self.subcircuits = collections.OrderedDict()
        # A Loads dictionary holding all the load circuits in this compound circuit
        self.loads = collections.OrderedDict()

    def generate(self):
        """ Generate method for base class must be overridden by child. """
        msg = "Function 'generate' must be overridden in class _CompoundCircuit."
        raise NotImplementedError(msg)
        
        
class _SwitchBlockMUX(_SizableCircuit):
    """ Switch Block MUX Class: Pass-transistor 2-level mux with output driver """
    
    def __init__(self, required_size, num_per_tile, use_tgate):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "sb_mux", use_tgate)
        # How big should this mux be (dictated by architecture specs)
        self.required_size = required_size 
        # How big did we make the mux (it is possible that we had to make the mux bigger for level sizes to work out, this is how big the mux turned out)
        self.implemented_size = -1
        # This is simply the implemented_size-required_size
        self.num_unused_inputs = -1
        # Number of switch block muxes in one FPGA tile
        self.num_per_tile = num_per_tile
        # Number of SRAM cells per mux
        self.sram_per_mux = -1
        # Size of the first level of muxing
        self.level1_size = -1
        # Size of the second level of muxing
        self.level2_size = -1
        # Delay weight in a representative critical path
        self.delay_weight = DELAY_WEIGHT_SB_MUX
        
        
    def generate(self, subcircuit_filename):
        """ 
        Generate switch block mux. 
        Calculates implementation specific details and write the SPICE subcircuit. 
        """
        
        print "Generating switch block mux"
        
        # Calculate level sizes and number of SRAMs per mux
        self.level2_size = int(math.sqrt(self.required_size))
        self.level1_size = int(math.ceil(float(self.required_size)/self.level2_size))
        self.implemented_size = self.level1_size*self.level2_size
        self.num_unused_inputs = self.implemented_size - self.required_size
        self.sram_per_mux = self.level1_size + self.level2_size
        
        
        # Call MUX generation function
        if not self.use_tgate :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2lvl_mux(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_L1_nmos"] = 3
            self.initial_transistor_sizes["ptran_" + self.name + "_L2_nmos"] = 4
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
        else :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2lvl_mux(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_nmos"] = 3
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_pmos"] = 3
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_nmos"] = 4
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_pmos"] = 4

        self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 8
        self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 4
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 10
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 20
       
        return self.initial_transistor_sizes


    def generate_top(self):
        """ Generate top level SPICE file """
        
        print "Generating top-level switch block mux"
        self.top_spice_path = top_level.generate_switch_block_top(self.name)
   
   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """
        
        # MUX area
        if not self.use_tgate :
            area = ((self.level1_size*self.level2_size)*area_dict["ptran_" + self.name + "_L1"] +
                    self.level2_size*area_dict["ptran_" + self.name + "_L2"] +
                    area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])
        else :
            area = ((self.level1_size*self.level2_size)*area_dict["tgate_" + self.name + "_L1"] +
                    self.level2_size*area_dict["tgate_" + self.name + "_L2"] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])

        # MUX area including SRAM
        area_with_sram = (area + (self.level1_size + self.level2_size)*area_dict["sram"])
        
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram
        
        # Update VPR areas
        if not self.use_tgate :
            area_dict["switch_mux_trans_size"] = area_dict["ptran_" + self.name + "_L1"]
            area_dict["switch_buf_size"] = area_dict["rest_" + self.name + ""] + area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]
        else :
            area_dict["switch_mux_trans_size"] = area_dict["tgate_" + self.name + "_L1"]
            area_dict["switch_buf_size"] = area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]


    def update_wires(self, width_dict, wire_lengths, wire_layers, ratio):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths
        wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_2"])/4
        wire_lengths["wire_" + self.name + "_L1"] = width_dict[self.name] * ratio
        wire_lengths["wire_" + self.name + "_L2"] = width_dict[self.name] * ratio
        
        # Update set wire layers
        wire_layers["wire_" + self.name + "_driver"] = 0
        wire_layers["wire_" + self.name + "_L1"] = 0
        wire_layers["wire_" + self.name + "_L2"] = 0
    
    
    def print_details(self, report_file):
        """ Print switch block details """

        utils.print_and_write(report_file, "  SWITCH BLOCK DETAILS:")
        utils.print_and_write(report_file, "  Style: two-level MUX")
        utils.print_and_write(report_file, "  Required MUX size: " + str(self.required_size) + ":1")
        utils.print_and_write(report_file, "  Implemented MUX size: " + str(self.implemented_size) + ":1")
        utils.print_and_write(report_file, "  Level 1 size = " + str(self.level1_size))
        utils.print_and_write(report_file, "  Level 2 size = " + str(self.level2_size))
        utils.print_and_write(report_file, "  Number of unused inputs = " + str(self.num_unused_inputs))
        utils.print_and_write(report_file, "  Number of MUXes per tile: " + str(self.num_per_tile))
        utils.print_and_write(report_file, "  Number of SRAM cells per MUX: " + str(self.sram_per_mux))
        utils.print_and_write(report_file, "")


class _ConnectionBlockMUX(_SizableCircuit):
    """ Connection Block MUX Class: Pass-transistor 2-level mux """
    
    def __init__(self, required_size, num_per_tile, use_tgate):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "cb_mux", use_tgate)
        # How big should this mux be (dictated by architecture specs)
        self.required_size = required_size 
        # How big did we make the mux (it is possible that we had to make the mux bigger for level sizes to work out, this is how big the mux turned out)
        self.implemented_size = -1
        # This is simply the implemented_size-required_size
        self.num_unused_inputs = -1
        # Number of switch block muxes in one FPGA tile
        self.num_per_tile = num_per_tile
        # Number of SRAM cells per mux
        self.sram_per_mux = -1
        # Size of the first level of muxing
        self.level1_size = -1
        # Size of the second level of muxing
        self.level2_size = -1
        # Delay weight in a representative critical path
        self.delay_weight = DELAY_WEIGHT_CB_MUX
        
    
    def generate(self, subcircuit_filename):
        print "Generating connection block mux"
        
        # Calculate level sizes and number of SRAMs per mux
        self.level2_size = int(math.sqrt(self.required_size))
        self.level1_size = int(math.ceil(float(self.required_size)/self.level2_size))
        self.implemented_size = self.level1_size*self.level2_size
        self.num_unused_inputs = self.implemented_size - self.required_size
        self.sram_per_mux = self.level1_size + self.level2_size
        
        # Call MUX generation function
        if not self.use_tgate :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2lvl_mux(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_" + self.name + "_L2_nmos"] = 2
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 6
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 12
        else :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2lvl_mux(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_pmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 6
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 12
       
        return self.initial_transistor_sizes


    def generate_top(self):
        print "Generating top-level connection block mux"
        self.top_spice_path = top_level.generate_connection_block_top(self.name)
        
   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """
            
        # MUX area
        if not self.use_tgate :
            area = ((self.level1_size*self.level2_size)*area_dict["ptran_" + self.name + "_L1"] +
                    self.level2_size*area_dict["ptran_" + self.name + "_L2"] +
                    area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])
        else :
            area = ((self.level1_size*self.level2_size)*area_dict["tgate_" + self.name + "_L1"] +
                    self.level2_size*area_dict["tgate_" + self.name + "_L2"] +
                    # area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])
        
        # MUX area including SRAM
        area_with_sram = (area + (self.level1_size + self.level2_size)*area_dict["sram"])
        
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram
        
        # Update VPR area numbers
        if not self.use_tgate :
            area_dict["ipin_mux_trans_size"] = area_dict["ptran_" + self.name + "_L1"]
            area_dict["cb_buf_size"] = area_dict["rest_" + self.name + ""] + area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]
        else :
            area_dict["ipin_mux_trans_size"] = area_dict["tgate_" + self.name + "_L1"]
            area_dict["cb_buf_size"] = area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]  
    
    def update_wires(self, width_dict, wire_lengths, wire_layers, ratio):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths
        wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_2"])/4
        wire_lengths["wire_" + self.name + "_L1"] = width_dict[self.name] * ratio
        wire_lengths["wire_" + self.name + "_L2"] = width_dict[self.name] * ratio
        
        # Update set wire layers
        wire_layers["wire_" + self.name + "_driver"] = 0
        wire_layers["wire_" + self.name + "_L1"] = 0
        wire_layers["wire_" + self.name + "_L2"] = 0    
        
   
    def print_details(self, report_file):
        """ Print connection block details """

        utils.print_and_write(report_file, "  CONNECTION BLOCK DETAILS:")
        utils.print_and_write(report_file, "  Style: two-level MUX")
        utils.print_and_write(report_file, "  Required MUX size: " + str(self.required_size) + ":1")
        utils.print_and_write(report_file, "  Implemented MUX size: " + str(self.implemented_size) + ":1")
        utils.print_and_write(report_file, "  Level 1 size = " + str(self.level1_size))
        utils.print_and_write(report_file, "  Level 2 size = " + str(self.level2_size))
        utils.print_and_write(report_file, "  Number of unused inputs = " + str(self.num_unused_inputs))
        utils.print_and_write(report_file, "  Number of MUXes per tile: " + str(self.num_per_tile))
        utils.print_and_write(report_file, "  Number of SRAM cells per MUX: " + str(self.sram_per_mux))
        utils.print_and_write(report_file, "")
        
        
class _LocalMUX(_SizableCircuit):
    """ Local Routing MUX Class: Pass-transistor 2-level mux with no driver """
    
    def __init__(self, required_size, num_per_tile, use_tgate, name = "local_mux"):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, name, use_tgate)
        # How big should this mux be (dictated by architecture specs)
        self.required_size = required_size 
        # How big did we make the mux (it is possible that we had to make the mux bigger for level sizes to work out, this is how big the mux turned out)
        self.implemented_size = -1
        # This is simply the implemented_size-required_size
        self.num_unused_inputs = -1
        # Number of switch block muxes in one FPGA tile
        self.num_per_tile = num_per_tile
        # Number of SRAM cells per mux
        self.sram_per_mux = -1
        # Size of the first level of muxing
        self.level1_size = -1
        # Size of the second level of muxing
        self.level2_size = -1
        # Delay weight in a representative critical path
        self.delay_weight = DELAY_WEIGHT_LOCAL_MUX

        # Calculate level sizes and number of SRAMs per mux
        self.level2_size = int(math.sqrt(self.required_size))
        self.level1_size = int(math.ceil(float(self.required_size)/self.level2_size))
        self.implemented_size = self.level1_size*self.level2_size
        self.num_unused_inputs = self.implemented_size - self.required_size
        self.sram_per_mux = self.level1_size + self.level2_size
        
    
    def generate(self, subcircuit_filename):
        print "Generating " + self.name
        
        if not self.use_tgate :
            # Call MUX generation function
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2lvl_mux_no_driver(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_" + self.name + "_L2_nmos"] = 2
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 2
        else :
            # Call MUX generation function
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2lvl_mux_no_driver(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_pmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 2
       
        return self.initial_transistor_sizes


    def generate_top(self):
        print "Generating top-level " + self.name
        self.top_spice_path = top_level.generate_local_mux_top(self.name)
        
   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # MUX area
        if not self.use_tgate :
            area = ((self.level1_size*self.level2_size)*area_dict["ptran_" + self.name + "_L1"] +
                    self.level2_size*area_dict["ptran_" + self.name + "_L2"] +
                    area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"])
        else :
            area = ((self.level1_size*self.level2_size)*area_dict["tgate_" + self.name + "_L1"] +
                    self.level2_size*area_dict["tgate_" + self.name + "_L2"] +
                    # area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"])
          
        # MUX area including SRAM
        area_with_sram = (area + (self.level1_size + self.level2_size)*area_dict["sram"])
          
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

    
    def update_wires(self, width_dict, wire_lengths, wire_layers, ratio):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths
        wire_lengths["wire_" + self.name + "_L1"] = width_dict[self.name] * ratio
        wire_lengths["wire_" + self.name + "_L2"] = width_dict[self.name] * ratio
        # Update wire layers
        wire_layers["wire_" + self.name + "_L1"] = 0
        wire_layers["wire_" + self.name + "_L2"] = 0  
        
   
    def print_details(self, report_file):
        """ Print local mux details """
    
        utils.print_and_write(report_file, "  LOCAL MUX '" + self.name + "' DETAILS:")
        utils.print_and_write(report_file, "  Style: two-level MUX")
        utils.print_and_write(report_file, "  Required MUX size: " + str(self.required_size) + ":1")
        utils.print_and_write(report_file, "  Implemented MUX size: " + str(self.implemented_size) + ":1")
        utils.print_and_write(report_file, "  Level 1 size = " + str(self.level1_size))
        utils.print_and_write(report_file, "  Level 2 size = " + str(self.level2_size))
        utils.print_and_write(report_file, "  Number of unused inputs = " + str(self.num_unused_inputs))
        utils.print_and_write(report_file, "  Number of MUXes per tile: " + str(self.num_per_tile))
        utils.print_and_write(report_file, "  Number of SRAM cells per MUX: " + str(self.sram_per_mux))
        utils.print_and_write(report_file, "")


class _LUTInputDriver(_SizableCircuit):
    """ LUT input driver class. LUT input drivers can optionally support register feedback.
        They can also be connected to FF register input select. 
        Thus, there are 4  types of LUT input drivers: "default", "default_rsel", "reg_fb" and "reg_fb_rsel".
        When a LUT input driver is created in the '__init__' function, it is given one of these types.
        All subsequent processes (netlist generation, area calculations, etc.) will use this type attribute.
        """

    def __init__(self, name, type, delay_weight, use_tgate, use_fluts, updates):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "lut_" + name + "_driver", use_tgate)
        # LUT input driver type ("default", "default_rsel", "reg_fb" and "reg_fb_rsel")
        self.type = type
        # Delay weight in a representative critical path
        self.delay_weight = delay_weight
        self.use_fluts = use_fluts
        self.updates = updates

        
    def generate(self, subcircuit_filename):
        """ Generate SPICE netlist based on type of LUT input driver. """
        if not self.use_tgate :
            self.transistor_names, self.wire_names = lut_subcircuits.generate_ptran_lut_driver(subcircuit_filename, self.name, self.type)
        else :
            self.transistor_names, self.wire_names = lut_subcircuits.generate_tgate_lut_driver(subcircuit_filename, self.name, self.type)
        
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        if not self.use_tgate :
            if self.type != "default":
                self.initial_transistor_sizes["inv_" + self.name + "_0_nmos"] = 2
                self.initial_transistor_sizes["inv_" + self.name + "_0_pmos"] = 2
            if self.type == "reg_fb" or self.type == "reg_fb_rsel":
                self.initial_transistor_sizes["ptran_" + self.name + "_0_nmos"] = 2
                self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            if self.type != "default":
                self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
                self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 2
        else :
            if self.type != "default":
                self.initial_transistor_sizes["inv_" + self.name + "_0_nmos"] = 2
                self.initial_transistor_sizes["inv_" + self.name + "_0_pmos"] = 2
            if self.type == "reg_fb" or self.type == "reg_fb_rsel":
                self.initial_transistor_sizes["tgate_" + self.name + "_0_nmos"] = 2
                self.initial_transistor_sizes["tgate_" + self.name + "_0_pmos"] = 2
            if self.type != "default":
                self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
                self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 2
               
        return self.initial_transistor_sizes


    def generate_top(self):
        """ Generate top-level SPICE file based on type of LUT input driver. """
        
        # Generate top level files based on what type of driver this is.
        self.top_spice_path = top_level.generate_lut_driver_top(self.name, self.type, self.updates)
        # And, generate the LUT driver + LUT path top level file. We use this file to measure total delay through the LUT.
        top_level.generate_lut_and_driver_top(self.name, self.type, self.use_tgate, self.use_fluts, self.updates)       
     
     
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. 
            We also return the area of this driver, which is calculated based on driver type. """
        
        area = 0.0
        
        if not self.use_tgate :  
            # Calculate area based on input type
            if self.type != "default":
                area += area_dict["inv_" + self.name + "_0"]
            if self.type == "reg_fb" or self.type == "reg_fb_rsel":
                area += 2*area_dict["ptran_" + self.name + "_0"]
                area += area_dict["rest_" + self.name]
            if self.type != "default":
                area += area_dict["inv_" + self.name + "_1"]
            area += area_dict["inv_" + self.name + "_2"]
        
        else :
            # Calculate area based on input type
            if self.type != "default":
                area += area_dict["inv_" + self.name + "_0"]
            if self.type == "reg_fb" or self.type == "reg_fb_rsel":
                area += 2*area_dict["tgate_" + self.name + "_0"]
            if self.type != "default":
                area += area_dict["inv_" + self.name + "_1"]
            area += area_dict["inv_" + self.name + "_2"]

        # Add SRAM cell if this is a register feedback input
        if self.type == "reg_fb" or self.type == "ref_fb_rsel":
            area += area_dict["sram"]
        
        # Calculate layout width
        width = math.sqrt(area)
        
        # Add to dictionaries
        area_dict[self.name] = area
        width_dict[self.name] = width
        
        return area
        

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict.
            Wires differ based on input type. """
        
        if not self.use_tgate :  
            # Update wire lengths and wire layers
            if self.type == "default_rsel" or self.type == "reg_fb_rsel":
                wire_lengths["wire_" + self.name + "_0_rsel"] = width_dict[self.name]/4 + width_dict["lut"] + width_dict["ff"]/4 
                wire_layers["wire_" + self.name + "_0_rsel"] = 0
            if self.type == "default_rsel":
                wire_lengths["wire_" + self.name + "_0_out"] = width_dict["inv_" + self.name + "_0"]/4 + width_dict["inv_" + self.name + "_2"]/4
                wire_layers["wire_" + self.name + "_0_out"] = 0
            if self.type == "reg_fb" or self.type == "reg_fb_rsel":
                wire_lengths["wire_" + self.name + "_0_out"] = width_dict["inv_" + self.name + "_0"]/4 + width_dict["ptran_" + self.name + "_0"]/4
                wire_layers["wire_" + self.name + "_0_out"] = 0
                wire_lengths["wire_" + self.name + "_0"] = width_dict["ptran_" + self.name + "_0"]
                wire_layers["wire_" + self.name + "_0"] = 0
            if self.type == "default":
                wire_lengths["wire_" + self.name] = width_dict["local_mux"]/4 + width_dict["inv_" + self.name + "_2"]/4
                wire_layers["wire_" + self.name] = 0
            else:
                wire_lengths["wire_" + self.name] = width_dict["inv_" + self.name + "_1"]/4 + width_dict["inv_" + self.name + "_2"]/4
                wire_layers["wire_" + self.name] = 0

        else :
            # Update wire lengths and wire layers
            if self.type == "default_rsel" or self.type == "reg_fb_rsel":
                wire_lengths["wire_" + self.name + "_0_rsel"] = width_dict[self.name]/4 + width_dict["lut"] + width_dict["ff"]/4 
                wire_layers["wire_" + self.name + "_0_rsel"] = 0
            if self.type == "default_rsel":
                wire_lengths["wire_" + self.name + "_0_out"] = width_dict["inv_" + self.name + "_0"]/4 + width_dict["inv_" + self.name + "_2"]/4
                wire_layers["wire_" + self.name + "_0_out"] = 0
            if self.type == "reg_fb" or self.type == "reg_fb_rsel":
                wire_lengths["wire_" + self.name + "_0_out"] = width_dict["inv_" + self.name + "_0"]/4 + width_dict["tgate_" + self.name + "_0"]/4
                wire_layers["wire_" + self.name + "_0_out"] = 0
                wire_lengths["wire_" + self.name + "_0"] = width_dict["tgate_" + self.name + "_0"]
                wire_layers["wire_" + self.name + "_0"] = 0
            if self.type == "default":
                wire_lengths["wire_" + self.name] = width_dict["local_mux"]/4 + width_dict["inv_" + self.name + "_2"]/4
                wire_layers["wire_" + self.name] = 0
            else:
                wire_lengths["wire_" + self.name] = width_dict["inv_" + self.name + "_1"]/4 + width_dict["inv_" + self.name + "_2"]/4
                wire_layers["wire_" + self.name] = 0
            

class _LUTInputNotDriver(_SizableCircuit):
    """ LUT input not-driver. This is the complement driver. """

    def __init__(self, name, type, delay_weight, use_tgate, updates):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "lut_" + name + "_driver_not", use_tgate)
        # LUT input driver type ("default", "default_rsel", "reg_fb" and "reg_fb_rsel")
        self.type = type
        # Delay weight in a representative critical path
        self.delay_weight = delay_weight

        self.updates = updates
   
    
    def generate(self, subcircuit_filename):
        """ Generate not-driver SPICE netlist """
        if not self.use_tgate :
            self.transistor_names, self.wire_names = lut_subcircuits.generate_ptran_lut_not_driver(subcircuit_filename, self.name)
        else :
            self.transistor_names, self.wire_names = lut_subcircuits.generate_tgate_lut_not_driver(subcircuit_filename, self.name)
        
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 2
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 2
       
        return self.initial_transistor_sizes


    def generate_top(self):
        """ Generate top-level SPICE file for LUT not driver """

        self.top_spice_path = top_level.generate_lut_driver_not_top(self.name, self.type, self.updates)
        
    
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. 
            We also return the area of this not_driver."""
        
        area = (area_dict["inv_" + self.name + "_1"] +
                area_dict["inv_" + self.name + "_2"])
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width
        
        return area
    
    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # Update wire lengths
        wire_lengths["wire_" + self.name] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_2"])/4
        # Update wire layers
        wire_layers["wire_" + self.name] = 0
    

class _LUTInput(_CompoundCircuit):
    """ LUT input. It contains a LUT input driver and a LUT input not driver (complement). 
        The muxing on the LUT input is defined here """

    def __init__(self, name, Rsel, Rfb, delay_weight, use_tgate, use_fluts, updates = 0):
        # Subcircuit name (should be driver letter like a, b, c...)
        _CompoundCircuit.__init__(self, name, use_tgate)
        # The type is either 'default': a normal input or 'reg_fb': a register feedback input 
        # In addition, the input can (optionally) drive the register input 'default_rsel' or do both 'reg_fb_rsel'
        # Therefore, there are 4 different types, which are controlled by Rsel and Rfb
        # The register select (Rsel) could only be one signal. While the feedback could be used with multiple signals
        if name in Rfb:
            if name in Rsel:
                self.type = "reg_fb_rsel"
            else:
                self.type = "reg_fb"
        else:
            if name in Rsel:
                self.type = "default_rsel"
            else:
                self.type = "default"

        # Create LUT input driver
        self.driver = _LUTInputDriver(name, self.type, delay_weight, use_tgate, use_fluts, updates)
        # Create LUT input not driver
        self.not_driver = _LUTInputNotDriver(name, self.type, delay_weight, use_tgate, updates)
        
        # input select mux used in updates 4
        if updates == 4 and name == 'c':
            self.input_select_mux = _MUX("c_input_mux", use_tgate, 2, False)
        # LUT input delays are the delays through the LUT for specific input (doesn't include input driver delay)
        self.tfall = 1
        self.trise = 1
        self.delay = 1
        self.delay_weight = delay_weight
        self.updates = updates
        
        
    def generate(self, subcircuit_filename):
        """ Generate both driver and not-driver SPICE netlists. """
        
        print "Generating lut " + self.name + "-input driver (" + self.type + ")"

        # Generate the driver
        init_tran_sizes = self.driver.generate(subcircuit_filename)
        # Generate the not driver
        init_tran_sizes.update(self.not_driver.generate(subcircuit_filename))

        # generate the input select mux
        if self.updates == 4 and self.name == 'c':
            init_tran_sizes.update(self.input_select_mux.generate(subcircuit_filename))

        return init_tran_sizes
  
            
    def generate_top(self):
        """ Generate top-level SPICE file for driver and not-driver. """
        
        print "Generating top-level lut " + self.name + "-input"
        
        # Generate the driver top
        self.driver.generate_top()
        # Generate the not driver top
        self.not_driver.generate_top()

        #generate the top of select mux
        if self.updates == 4 and self.name == 'c':
            self.mux_top_spice_path = top_level.generate_input_select_mux_top("lut_" + self.name + "_driver", self.use_tgate, self.use_finfet)
            self.input_select_mux.top_spice_path = self.mux_top_spice_path

     
    def update_area(self, area_dict, width_dict):
        """ Update area. We update the area of the the driver and the not driver by calling area update functions
            inside these objects. We also return the total area of this input driver."""        
        
        # Calculate area of driver
        driver_area = self.driver.update_area(area_dict, width_dict)
        # Calculate area of not driver
        not_driver_area = self.not_driver.update_area(area_dict, width_dict)
        # Add the input select mux
        if self.updates == 4 and self.name == 'c':
            mux_area = self.input_select_mux.update_area(area_dict, width_dict)
        else:
            mux_area = 0
        # Return the sum
        return driver_area + not_driver_area + 2*mux_area
    
    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers for input driver and not_driver """
        
        # Update driver wires
        self.driver.update_wires(width_dict, wire_lengths, wire_layers)
        # Update not driver wires
        self.not_driver.update_wires(width_dict, wire_lengths, wire_layers)
        # Update the input select mux wires
        if self.updates == 4 and self.name == 'c':
            self.input_select_mux.update_wires(width_dict, wire_lengths, wire_layers)
        
        
    def print_details(self, report_file):
        """ Print LUT input driver details """
        
        utils.print_and_write(report_file, "  LUT input " + self.name + " type: " + self.type)



class _LUTInputDriverLoad:
    """ LUT input driver load. This load consists of a wire as well as the gates
        of a particular level in the LUT. """

    def __init__(self, name, use_tgate, use_fluts, updates):
        self.name = name
        self.use_tgate = use_tgate
        self.use_fluts = use_fluts
        self.updates = updates
        
        
    def generate(self, subcircuit_filename, K):
        
        print "Generating LUT " + self.name + "-input driver load"
        
        if not self.use_tgate :
            # Call generation function based on input
            self.wire_names = lut_subcircuits.generate_ptran_lut_driver_load(subcircuit_filename, self.name, K, self.use_fluts, self.updates)
        else :
            self.wire_names = lut_subcircuits.generate_tgate_lut_driver_load(subcircuit_filename, self.name, K, self.use_fluts, self.updates)


    def update_wires(self, width_dict, wire_lengths, wire_layers, ratio):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths
        wire_lengths["wire_lut_" + self.name + "_driver_load"] = width_dict["lut"] * ratio      

        # if new design, add the wire connecting the e and f inputs to the second carry chain
        if self.updates in (2, 3) and self.name in ("e", "f"):
            # Update wire lengths
            wire_lengths["wire_lut_" + self.name + "_driver_to_cc2"] = width_dict["lut"] * ratio + width_dict["carry_chain"]      

        # adding the wire connecting inputs d, e, and f to the input select mux
        if self.updates == 4 and self.name in ('d', 'e', 'f'):
            wire_lengths[utils.wire_name("lut_" + self.name + "_driver", "input_select")] = width_dict["lut"] * ratio

        for wire in self.wire_names:
            wire_layers[wire] = 0
        
        
    def print_details(self):
        print "LUT input driver load details."
   
        
class _LUT(_SizableCircuit):
    """ Lookup table. """

    def __init__(self, K, Rsel, Rfb, use_tgate, use_finfet, use_fluts, min_tran_width, updates):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "lut", use_tgate, use_finfet)
        
        self.use_fluts = use_fluts
        # Size of LUT
        self.K = K
        # Dictionary of input drivers (keys: "a", "b", etc...)
        self.input_drivers = {}
        # Dictionary of input driver loads
        self.input_driver_loads = {}
        # initial transistor sizes
        self.initial_transistor_sizes = {}
        # Delay weight in a representative critical path
        # sum the delay weights of the used inputs
        self.delay_weight = sum(DELAY_WEIGHT_LUT_INPUTS[:K])
        # Boolean to indicate we are implementing the new architecture
        self.updates = updates

        self.min_tran_width = min_tran_width

        self.lutSize = K
        # for the new design the 6-LUT is fractured into 4 4-LUTs with only
        # inputs a and b used as feedback inputs and register select inputs
        # design 4, with level 3 of fracturability 
        if updates == 4:
            self.lutSize -= 3
            Rsel = 'ab'
            Rfb = 'ab'
        elif updates:
            self.lutSize -= 2
            Rsel = 'ab'
            Rfb = 'ab'
        elif use_fluts: 
            self.lutSize -= 1
          
        # Create a LUT input driver and load for each LUT input
        # note that even in case of fluts all the K input drivers are
        # needed since the K represents the lut size before fracturing
        # Note: for the new design we still have 6 distincitve inputs
        # which are a, b, c, d, e, and f. Keeping in mind that c and d
        # are actually repeated in the actual design (c0, c1 and d0, d1)
        # which should be taken into consideration when calculating the
        # area of the drivers
        for i in range(97, K+97):
            name = chr(i)
            delay_weight = DELAY_WEIGHT_LUT_INPUTS[i-97]
            self.input_drivers[name] = _LUTInput(name, Rsel, Rfb, delay_weight, use_tgate, use_fluts, updates)
            self.input_driver_loads[name] = _LUTInputDriverLoad(name, use_tgate, use_fluts, updates)


    
    def generate(self, subcircuit_filename):
        """ Generate LUT SPICE netlist based on LUT size. """
        
        # Generate LUT differently based on K
        if self.lutSize == 6:
            self.initial_transistor_sizes = self._generate_6lut(subcircuit_filename, self.min_tran_width, self.use_tgate, self.use_finfet, self.use_fluts)
        elif self.lutSize == 5:
            self.initial_transistor_sizes = self._generate_5lut(subcircuit_filename, self.min_tran_width, self.use_tgate, self.use_finfet, self.use_fluts)
        elif self.lutSize == 4:
            self.initial_transistor_sizes = self._generate_4lut(subcircuit_filename, self.min_tran_width, self.use_tgate, self.use_finfet, self.use_fluts)
        elif self.lutSize == 3:
            self.initial_transistor_sizes = self._generate_3lut(subcircuit_filename, self.min_tran_width, self.use_tgate, self.use_finfet, self.use_fluts)

        # generate all the drivers added to the input drivers list in the constructor
        for name in self.input_drivers:
            # the initial transistor sizes wasn't updated with the dictionary comming from the input drivers
            self.initial_transistor_sizes.update(self.input_drivers[name].generate(subcircuit_filename))
            self.input_driver_loads[name].generate(subcircuit_filename, self.K)

        return self.initial_transistor_sizes


    def generate_top(self):
        print "Generating top-level lut"
        
        if self.lutSize == 6:
            self.top_spice_path = top_level.generate_lut6_top(self.name, self.use_tgate)
        elif self.lutSize == 5:
            self.top_spice_path = top_level.generate_lut5_top(self.name, self.use_tgate)
        elif self.lutSize == 4:
            self.top_spice_path = top_level.generate_lut4_top(self.name, self.use_tgate, self.updates)
        elif self.lutSize == 3:
            self.top_spice_path = top_level.generate_lut3_top(self.name, self.use_tgate, self.updates)
            
        # Generate top-level driver files
        for input_driver in self.input_drivers.values():
            input_driver.generate_top()
   
   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. 
            We update the area of the LUT as well as the area of the LUT input drivers. """        

        area = 0.0
        
        if not self.use_tgate :
            # Calculate area (differs with different values of K)
            if self.lutSize == 6:    
                area += (64*area_dict["inv_lut_0sram_driver_2"] + 
                         64*area_dict["ptran_lut_L1"] + 
                         32*area_dict["ptran_lut_L2"] + 
                         16*area_dict["ptran_lut_L3"] +      
                         8*area_dict["rest_lut_int_buffer"] + 
                         8*area_dict["inv_lut_int_buffer_1"] + 
                         8*area_dict["inv_lut_int_buffer_2"] + 
                         8*area_dict["ptran_lut_L4"] + 
                         4*area_dict["ptran_lut_L5"] + 
                         2*area_dict["ptran_lut_L6"] + 
                         area_dict["rest_lut_out_buffer"] + 
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         64*area_dict["sram"])
            elif self.lutSize == 5:
                area += (32*area_dict["inv_lut_0sram_driver_2"] + 
                         32*area_dict["ptran_lut_L1"] + 
                         16*area_dict["ptran_lut_L2"] + 
                         8*area_dict["ptran_lut_L3"] + 
                         4*area_dict["rest_lut_int_buffer"] + 
                         4*area_dict["inv_lut_int_buffer_1"] + 
                         4*area_dict["inv_lut_int_buffer_2"] + 
                         4*area_dict["ptran_lut_L4"] + 
                         2*area_dict["ptran_lut_L5"] +  
                         area_dict["rest_lut_out_buffer"] + 
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         32*area_dict["sram"])
            elif self.lutSize == 4:
                area += (16*area_dict["inv_lut_0sram_driver_2"] + 
                         16*area_dict["ptran_lut_L1"] + 
                         8*area_dict["ptran_lut_L2"] + 
                         4*area_dict["rest_lut_int_buffer"] + 
                         4*area_dict["inv_lut_int_buffer_1"] + 
                         4*area_dict["inv_lut_int_buffer_2"] +
                         4*area_dict["ptran_lut_L3"] + 
                         2*area_dict["ptran_lut_L4"] +   
                         area_dict["rest_lut_out_buffer"] + 
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         16*area_dict["sram"])
            elif self.lutSize == 3:
                area += (8*area_dict["inv_lut_0sram_driver_2"] + 
                         8*area_dict["ptran_lut_L1"] + 
                         4*area_dict["ptran_lut_L2"] + 
                         2*area_dict["ptran_lut_L3"] + 
                         area_dict["rest_lut_out_buffer"] + 
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         8*area_dict["sram"])

        else :
            # Calculate area (differs with different values of K)
            if self.lutSize == 6:    
                area += (64*area_dict["inv_lut_0sram_driver_2"] + 
                         64*area_dict["tgate_lut_L1"] + 
                         32*area_dict["tgate_lut_L2"] + 
                         16*area_dict["tgate_lut_L3"] + 
                         8*area_dict["inv_lut_int_buffer_1"] + 
                         8*area_dict["inv_lut_int_buffer_2"] + 
                         8*area_dict["tgate_lut_L4"] + 
                         4*area_dict["tgate_lut_L5"] + 
                         2*area_dict["tgate_lut_L6"] + 
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         64*area_dict["sram"])
            elif self.lutSize == 5:
                area += (32*area_dict["inv_lut_0sram_driver_2"] + 
                         32*area_dict["tgate_lut_L1"] + 
                         16*area_dict["tgate_lut_L2"] + 
                         8*area_dict["tgate_lut_L3"] + 
                         4*area_dict["inv_lut_int_buffer_1"] + 
                         4*area_dict["inv_lut_int_buffer_2"] + 
                         4*area_dict["tgate_lut_L4"] + 
                         2*area_dict["tgate_lut_L5"] +  
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         32*area_dict["sram"])
            elif self.lutSize == 4:
                area += (16*area_dict["inv_lut_0sram_driver_2"] + 
                         16*area_dict["tgate_lut_L1"] + 
                         8*area_dict["tgate_lut_L2"] + 
                         4*area_dict["inv_lut_int_buffer_1"] + 
                         4*area_dict["inv_lut_int_buffer_2"] +
                         4*area_dict["tgate_lut_L3"] + 
                         2*area_dict["tgate_lut_L4"] +   
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         16*area_dict["sram"])
            elif self.lutSize == 3:
                area += (8*area_dict["inv_lut_0sram_driver_2"] + 
                         8*area_dict["tgate_lut_L1"] + 
                         4*area_dict["tgate_lut_L2"] + 
                         2*area_dict["tgate_lut_L3"] + 
                         area_dict["inv_lut_out_buffer_1"] + 
                         area_dict["inv_lut_out_buffer_2"] +
                         8*area_dict["sram"])
        
        # TODO: do this in the ble class to avoid writing the names of the instances manully
        # TODO: Should the inputs to the 2 fluts come from the same driver or should they 
        # have different drivers?
        if not self.updates:
            if self.use_fluts:
                area = 2*area
                area = area + area_dict["flut_mux"]
        elif self.updates in (1, 2, 3, 10):
            area *= 4
            area += 3*area_dict["fmux_l1"]
            area += area_dict["fmux_l2"]
        elif self.updates == 4:
            area *= 8
            area += 4*area_dict["fmux_l1"]
            area += 3*area_dict["fmux_l2"]
            area += area_dict["fmux_l3"]

        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width
        
        # Calculate LUT driver areas
        # for the new design the drivers for c and d are present twice since
        # the design has c0 and d0 inputs in addition to c1 and d1.
        total_lut_area = 0.0
        for driver_name, input_driver in self.input_drivers.iteritems():
            driver_area = input_driver.update_area(area_dict, width_dict)
            if self.updates and (driver_name == 'c' or driver_name == 'd'):
                total_lut_area += 2*driver_area
            else:
                total_lut_area +=  driver_area
       
        # Now we calculate total LUT area
        total_lut_area += area_dict[self.name]

        area_dict["lut_and_drivers"] = total_lut_area
        width_dict["lut_and_drivers"] = math.sqrt(total_lut_area)
        
        return total_lut_area
    

    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        if not self.use_tgate :
            if self.lutSize == 6:        
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["ptran_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L3"] = 4*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_int_buffer"] = (width_dict["inv_lut_int_buffer_1"] + width_dict["inv_lut_int_buffer_2"])/4
                wire_lengths["wire_lut_int_buffer_out"] = (width_dict["inv_lut_int_buffer_2"] + width_dict["ptran_lut_L4"])/4
                wire_lengths["wire_lut_L4"] = 8*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L5"] = 16*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L6"] = 32*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4
              
            elif self.lutSize == 5:
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["ptran_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L3"] = 4*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_int_buffer"] = (width_dict["inv_lut_int_buffer_1"] + width_dict["inv_lut_int_buffer_2"])/4
                wire_lengths["wire_lut_int_buffer_out"] = (width_dict["inv_lut_int_buffer_2"] + width_dict["ptran_lut_L4"])/4
                wire_lengths["wire_lut_L4"] = 8*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L5"] = 16*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4

            elif self.lutSize == 4:
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["ptran_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_int_buffer"] = (width_dict["inv_lut_int_buffer_1"] + width_dict["inv_lut_int_buffer_2"])/4
                wire_lengths["wire_lut_int_buffer_out"] = (width_dict["inv_lut_int_buffer_2"] + width_dict["ptran_lut_L4"])/4
                wire_lengths["wire_lut_L3"] = 4*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L4"] = 8*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4

            elif self.lutSize == 3:
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["ptran_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_L3"] = 4*width_dict["ptran_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4

        else:
            if self.lutSize == 6:        
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["tgate_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L3"] = 4*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_int_buffer"] = (width_dict["inv_lut_int_buffer_1"] + width_dict["inv_lut_int_buffer_2"])/4
                wire_lengths["wire_lut_int_buffer_out"] = (width_dict["inv_lut_int_buffer_2"] + width_dict["tgate_lut_L4"])/4
                wire_lengths["wire_lut_L4"] = 8*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L5"] = 16*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L6"] = 32*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4
              
            elif self.lutSize == 5:
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["tgate_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L3"] = 4*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_int_buffer"] = (width_dict["inv_lut_int_buffer_1"] + width_dict["inv_lut_int_buffer_2"])/4
                wire_lengths["wire_lut_int_buffer_out"] = (width_dict["inv_lut_int_buffer_2"] + width_dict["tgate_lut_L4"])/4
                wire_lengths["wire_lut_L4"] = 8*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L5"] = 16*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4
                
            elif self.lutSize == 4:
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["tgate_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_int_buffer"] = (width_dict["inv_lut_int_buffer_1"] + width_dict["inv_lut_int_buffer_2"])/4
                wire_lengths["wire_lut_int_buffer_out"] = (width_dict["inv_lut_int_buffer_2"] + width_dict["tgate_lut_L4"])/4
                wire_lengths["wire_lut_L3"] = 4*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L4"] = 8*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4

            elif self.lutSize == 3:
                # Update wire lengths
                wire_lengths["wire_lut_sram_driver"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["inv_lut_0sram_driver_2"])/4
                wire_lengths["wire_lut_sram_driver_out"] = (width_dict["inv_lut_0sram_driver_2"] + width_dict["tgate_lut_L1"])/4
                wire_lengths["wire_lut_L1"] = width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L2"] = 2*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_L3"] = 4*width_dict["tgate_lut_L1"]
                wire_lengths["wire_lut_out_buffer"] = (width_dict["inv_lut_out_buffer_1"] + width_dict["inv_lut_out_buffer_2"])/4

          
        # Update input driver and input driver load wires
        for name in self.input_drivers:
            self.input_drivers[name].update_wires(width_dict, wire_lengths, wire_layers) 
            self.input_driver_loads[name].update_wires(width_dict, wire_lengths, wire_layers, lut_ratio)

        #update wire layers
        for wire in self.wire_names:
            wire_layers[wire] = 0
    
        
    def print_details(self, report_file):
        """ Print LUT details """
    
        utils.print_and_write(report_file, "  LUT DETAILS:")
        utils.print_and_write(report_file, "  Style: Fully encoded MUX tree")
        utils.print_and_write(report_file, "  Size: " + str(self.K) + "-LUT")
        utils.print_and_write(report_file, "  Internal buffering: 2-stage buffer betweens levels 3 and 4")
        utils.print_and_write(report_file, "  Isolation inverters between SRAM and LUT inputs")
        utils.print_and_write(report_file, "")
        utils.print_and_write(report_file, "  LUT INPUT DRIVER DETAILS:")
        for driver_name, input_driver in self.input_drivers.iteritems():
            input_driver.print_details(report_file)
        utils.print_and_write(report_file,"")
        
    
    def _generate_6lut(self, subcircuit_filename, min_tran_width, use_tgate, use_finfet, use_fluts):
        """ This function created the lut subcircuit and all the drivers and driver not subcircuits """
        print "Generating 6-LUT"

        # Call the generation function
        if not use_tgate :
            # use pass transistors
            self.transistor_names, self.wire_names = lut_subcircuits.generate_ptran_lut6(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["inv_lut_0sram_driver_2_pmos"] = 6
            self.initial_transistor_sizes["ptran_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["rest_lut_int_buffer_pmos"] = 1
            self.initial_transistor_sizes["ptran_lut_L4_nmos"] = 3
            self.initial_transistor_sizes["ptran_lut_L5_nmos"] = 3
            self.initial_transistor_sizes["ptran_lut_L6_nmos"] = 3
            self.initial_transistor_sizes["rest_lut_out_buffer_pmos"] = 1

        else :
            # use transmission gates
            self.transistor_names, self.wire_names = lut_subcircuits.generate_tgate_lut6(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["tgate_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L1_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L4_nmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L4_pmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L5_nmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L5_pmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L6_nmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L6_pmos"] = 3

        # common transistors between tgate and ptran based desings
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_pmos"] = 6

        self.initial_transistor_sizes["inv_lut_int_buffer_1_nmos"] = 2
        self.initial_transistor_sizes["inv_lut_int_buffer_1_pmos"] = 2
        self.initial_transistor_sizes["inv_lut_int_buffer_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_int_buffer_2_pmos"] = 6

        self.initial_transistor_sizes["inv_lut_out_buffer_1_nmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_1_pmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_out_buffer_2_pmos"] = 6
        
        return self.initial_transistor_sizes

        
    def _generate_5lut(self, subcircuit_filename, min_tran_width, use_tgate, use_finfet, use_fluts):
        """ This function created the lut subcircuit and all the drivers and driver not subcircuits """
        print "Generating 5-LUT"
        
        # Call the generation function
        if not use_tgate :
            # use pass transistor
            self.transistor_names, self.wire_names = lut_subcircuits.generate_ptran_lut5(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["ptran_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["rest_lut_int_buffer_pmos"] = 1
            self.initial_transistor_sizes["ptran_lut_L4_nmos"] = 3
            self.initial_transistor_sizes["ptran_lut_L5_nmos"] = 3
            self.initial_transistor_sizes["rest_lut_out_buffer_pmos"] = 1
        else :
            # use transmission gates
            self.transistor_names, self.wire_names = lut_subcircuits.generate_tgate_lut5(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["tgate_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L1_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L4_nmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L4_pmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L5_nmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L5_pmos"] = 3

        # common transistors between tgate and ptran based designs
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_pmos"] = 6

        self.initial_transistor_sizes["inv_lut_int_buffer_1_nmos"] = 2
        self.initial_transistor_sizes["inv_lut_int_buffer_1_pmos"] = 2
        self.initial_transistor_sizes["inv_lut_int_buffer_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_int_buffer_2_pmos"] = 6

        self.initial_transistor_sizes["inv_lut_out_buffer_1_nmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_1_pmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_out_buffer_2_pmos"] = 6
        
        return self.initial_transistor_sizes

  
    def _generate_4lut(self, subcircuit_filename, min_tran_width, use_tgate, use_finfet, use_fluts):
        """ This function created the lut subcircuit and all the drivers and driver not subcircuits """
        print "Generating 4-LUT"
        
        # Call the generation function
        if not use_tgate :
            # use pass transistor
            self.transistor_names, self.wire_names = lut_subcircuits.generate_ptran_lut4(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["ptran_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["rest_lut_int_buffer_pmos"] = 1
            self.initial_transistor_sizes["ptran_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L4_nmos"] = 3
            self.initial_transistor_sizes["rest_lut_out_buffer_pmos"] = 1
        else :
            # use transmission gates
            self.transistor_names, self.wire_names = lut_subcircuits.generate_tgate_lut4(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["tgate_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L1_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L4_nmos"] = 3
            self.initial_transistor_sizes["tgate_lut_L4_pmos"] = 3

        # common transistors between tgate and ptran based designs
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_pmos"] = 6

        self.initial_transistor_sizes["inv_lut_int_buffer_1_nmos"] = 2
        self.initial_transistor_sizes["inv_lut_int_buffer_1_pmos"] = 2
        self.initial_transistor_sizes["inv_lut_int_buffer_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_int_buffer_2_pmos"] = 6

        self.initial_transistor_sizes["inv_lut_out_buffer_1_nmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_1_pmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_out_buffer_2_pmos"] = 6

        return self.initial_transistor_sizes


    def _generate_3lut(self, subcircuit_filename, min_tran_width, use_tgate, use_finfet, use_fluts):
        """ This function created the lut subcircuit and all the drivers and driver not subcircuits """
        print "Generating 3-LUT"
        
        # Call the generation function
        if not use_tgate:
            # use pass transistor
            self.transistor_names, self.wire_names = lut_subcircuits.generate_ptran_lut3(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["ptran_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["ptran_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["rest_lut_out_buffer_pmos"] = 1
        else:
            # use transmission gates
            self.transistor_names, self.wire_names = lut_subcircuits.generate_tgate_lut3(subcircuit_filename, min_tran_width, use_finfet)
            # Give initial transistor sizes
            self.initial_transistor_sizes["tgate_lut_L1_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L1_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L2_pmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_nmos"] = 2
            self.initial_transistor_sizes["tgate_lut_L3_pmos"] = 2

        # common transistors between tgate and ptran based designs
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_0sram_driver_2_pmos"] = 6

        self.initial_transistor_sizes["inv_lut_out_buffer_1_nmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_1_pmos"] = 2
        self.initial_transistor_sizes["inv_lut_out_buffer_2_nmos"] = 4
        self.initial_transistor_sizes["inv_lut_out_buffer_2_pmos"] = 6

        return self.initial_transistor_sizes



class _CarryChainMux(_SizableCircuit):
    """ Carry Chain Multiplexer class.    """

    def __init__(self, use_finfet, use_fluts, use_tgate):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "carry_chain_mux", use_tgate, use_finfet)
          
        self.use_fluts = use_fluts
        

    def generate(self, subcircuit_filename):
        """ Generate the SPICE netlists."""  

        if not self.use_tgate :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2_to_1_mux(subcircuit_filename, self.name)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_nmos"] = 2
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
        else :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2_to_1_mux(subcircuit_filename, self.name)      
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_pmos"] = 2

        self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 5
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 5
       
        return self.initial_transistor_sizes

    def generate_top(self):       

        print "Generating top-level " + self.name
        self.top_spice_path = top_level.generate_cc_mux_top(self.name, self.use_tgate)

    def update_area(self, area_dict, width_dict):

        if not self.use_tgate :
            area = (2*area_dict["ptran_" + self.name] +
                    area_dict["rest_" + self.name] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])
        else :
            area = (2*area_dict["tgate_" + self.name] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])

        area = area + area_dict["sram"]
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width

        return area
                

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire of member objects. """
        # Update wire lengths
        if not self.use_tgate :
            wire_lengths["wire_" + self.name] = width_dict["ptran_" + self.name]
        else :
            wire_lengths["wire_" + self.name] = width_dict["tgate_" + self.name]

        wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_1"])/4
        
        # Update wire layers
        wire_layers["wire_" + self.name] = 0
        wire_layers["wire_" + self.name + "_driver"] = 0


class _CarryChainPer(_SizableCircuit):
    """ Carry Chain Peripherals class. Used to measure the delay from the Cin to Sout.  """

    number = 1

    def __init__(self, use_finfet, carry_chain_type, N, FAs_per_flut, use_tgate, updates):
         # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "carry_chain_perf", use_tgate, use_finfet)
        # carry chain type either ripple or skip
        self.carry_chain_type = carry_chain_type
        # handled in the check_arch_params funciton in utils.py
        # assert FAs_per_flut <= 2
        self.FAs_per_flut = FAs_per_flut
        # how many Fluts do we have in a cluster?
        self.N = N        
        # Holds the updates number
        self.updates = updates
        # Instance id
        self.id = _CarryChainPer.number
        # if this is update 2 and this is the second adder add 2 to its name
        if updates and self.id == 2:
            self.name += str(self.id)

        # Increment the static variable number each time a new instance is created
        _CarryChainPer.number += 1


    def generate(self, subcircuit_filename):
        """ Generate the SPICE netlists."""  
        print("Generatign " + self.name)

        # if type is skip, we need to generate two levels of nand + not for the and tree
        # if type is ripple, we need to add the delay of one inverter for the final sum.
        self.transistor_names, self.wire_names = lut_subcircuits.generate_carry_chain_perf_ripple(subcircuit_filename, self.name, self.use_finfet)
        self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1

        if self.updates in (1, 4, 10):
            self.wire_names.append(utils.wire_name("cc_sout", "reg3_sel"))
        # if this is updats 2 and id 1 no need to create the wire from cc1 to cc2 becuase its already created in the carrychain class
        elif self.id == 2:
            if self.updates == 2:
                self.wire_names.append(utils.wire_name("cc2_sout", "reg3_sel"))
            elif self.updates == 3:
                self.wire_names.append(utils.wire_name("cc2_sout", "reg4_sel"))
                
    
        return self.initial_transistor_sizes
    def generate_top(self):
        """ Generate Top-level Evaluation Path for the Carry chain """
        print("Generating top-level " + self.name)

        if self.carry_chain_type == "ripple":
            self.top_spice_path = top_level.generate_carry_chain_ripple_top(self.name, self.updates, self.id)
        else:
            self.top_spice_path = top_level.generate_carry_chain_skip_top(self.name, self.use_tgate)


    def update_area(self, area_dict, width_dict):
        """ Calculate Carry Chain area and update dictionaries. """

        area = area_dict["inv_"+self.name+"_1"]    
        area_with_sram = area
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width


    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        # TODO: revise this line 
        if self.updates in (1, 4, 10):
            wire_lengths[utils.wire_name("cc_sout", "reg3_sel")] = width_dict["lut"]
            wire_layers[utils.wire_name("cc_sout", "reg3_sel")] = 0
        # if this is updats 2 and id 1 no need to create the wire from cc1 to cc2 becuase its already created in the carrychain class
        elif self.id == 2:
            if self.updates == 2:
                wire_lengths[utils.wire_name("cc2_sout", "reg3_sel")] = width_dict["lut"]/2
                wire_layers[utils.wire_name("cc2_sout", "reg3_sel")] = 0
            elif self.updates == 3:
                wire_lengths[utils.wire_name("cc2_sout", "reg4_sel")] = width_dict["lut"]/2
                wire_layers[utils.wire_name("cc2_sout", "reg4_sel")] = 0



class _CarryChain(_SizableCircuit):
    """ Carry Chain class.    """

    number = 1

    def __init__(self, use_finfet, carry_chain_type, N, FAs_per_flut, updates = 0):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "carry_chain", False, use_finfet)
        # ripple or skip?
        self.carry_chain_type = carry_chain_type
        # added to the check_arch_params function
        # assert FAs_per_flut <= 2      
        self.FAs_per_flut = FAs_per_flut
        # how many Fluts do we have in a cluster?
        self.N = N
        # Boolean for applying updates
        self.updates = updates
        # Instance id
        self.id = _CarryChain.number
        # if this is update 2 and this is the second adder add 2 to its name
        if updates and self.id == 2:
            self.name += str(self.id)
        # Increment the static varible number with one each time you create instance
        _CarryChain.number += 1

        self.in_sout_delay = 1
        self.in_cout_delay = 1


    def generate(self, subcircuit_filename):
        """ Generate Carry chain SPICE netlists."""  
        print("Generating " + self.name)

        self.transistor_names, self.wire_names = lut_subcircuits.generate_full_adder_simplified(subcircuit_filename, self.name, self.use_finfet)

        # if type is skip, we need to generate two levels of nand + not for the and tree
        # if type is ripple, we need to add the delay of one inverter for the final sum.

        self.initial_transistor_sizes["inv_"+self.name+"_1_nmos"] = 1
        self.initial_transistor_sizes["inv_"+self.name+"_1_pmos"] = 1
        self.initial_transistor_sizes["inv_"+self.name+"_2_nmos"] = 1
        self.initial_transistor_sizes["inv_"+self.name+"_2_pmos"] = 1
        self.initial_transistor_sizes["tgate_"+self.name+"_1_nmos"] = 1
        self.initial_transistor_sizes["tgate_"+self.name+"_1_pmos"] = 1
        self.initial_transistor_sizes["tgate_"+self.name+"_2_nmos"] = 1
        self.initial_transistor_sizes["tgate_"+self.name+"_2_pmos"] = 1


        if self.updates:
            if self.id == 1:
                 # this wire is needed in the flut mux simulations of the new architecture
                self.wire_names.append(utils.wire_name("lut", self.name))
            elif self.id == 2:
                # this wire is needed in the simulation of the carry chain perf of carry chain 1
                self.wire_names.append(utils.wire_name("cc1_sout", self.name))
                if self.updates == 3:
                    self.wire_names.append(utils.wire_name("cc1_sout", "reg4_sel"))

        return self.initial_transistor_sizes


    def generate_top(self):
        """ Generate Top-level Evaluation Path for Carry chain """
        print("Generating top-level " + self.name)
        self.top_spice_path = top_level.generate_carrychain_top(self.name)

        if self.updates:
            self.top_meas = top_level.generate_cc_in_out_top(self.name, self.use_tgate, self.updates, self.id)


    def update_area(self, area_dict, width_dict):
        """ Calculate Carry Chain area including the inverter 
            at the Sout port and update dictionaries. """

        area = area_dict["inv_"+self.name+"_1"] * 2 + area_dict["inv_"+self.name+"_2"] + area_dict["tgate_"+self.name+"_1"] * 4 + area_dict["tgate_"+self.name+"_2"] * 4

        if self.updates in (2, 3) and self.id == 2:
            area += area_dict["carry_chain_perf2"]
        else:
            area += area_dict["carry_chain_perf"]

        area_with_sram = area
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        if self.FAs_per_flut ==2:
            wire_lengths["wire_" + self.name + "_1"] = width_dict["lut_and_drivers"] # Wire for input A
        else:
            wire_lengths["wire_" + self.name + "_1"] = width_dict[self.name] # Wire for input A
        wire_layers["wire_" + self.name + "_1"] = 0
        wire_lengths["wire_" + self.name + "_2"] = width_dict[self.name] # Wire for input B
        wire_layers["wire_" + self.name + "_2"] = 0

        # Wires for Cin and Cout are shorter since the number of adders per ALM is doubled
        if self.updates == 4:
            wire_lengths["wire_" + self.name + "_3"] = width_dict["logic_cluster"]/(8 * self.N) # Wire for input Cin
            wire_layers["wire_" + self.name + "_3"] = 0
            wire_lengths["wire_" + self.name + "_4"] = width_dict["logic_cluster"]/(8 * self.N) # Wire for output Cout
            wire_layers["wire_" + self.name + "_4"] = 0
        else:
            if self.FAs_per_flut ==1:
                wire_lengths["wire_" + self.name + "_3"] = width_dict["logic_cluster"]/(2 * self.N) # Wire for input Cin
            else:
                wire_lengths["wire_" + self.name + "_3"] = width_dict["logic_cluster"]/(4 * self.N) # Wire for input Cin
            wire_layers["wire_" + self.name + "_3"] = 0
            if self.FAs_per_flut ==1:
                wire_lengths["wire_" + self.name + "_4"] = width_dict["logic_cluster"]/(2 * self.N) # Wire for output Cout
            else:
                wire_lengths["wire_" + self.name + "_4"] = width_dict["logic_cluster"]/(4 * self.N) # Wire for output Cout
            wire_layers["wire_" + self.name + "_4"] = 0

        wire_lengths["wire_" + self.name + "_5"] = width_dict[self.name] # Wire for output Sum
        wire_layers["wire_" + self.name + "_5"] = 0

        # TODO: revise those values
        # this wire is needed in the flut mux simulation for the new architecture
        if self.updates:
            if self.id == 1:
                wire_lengths[utils.wire_name("lut", self.name)] = width_dict["fmux_l1"]
                wire_layers[utils.wire_name("lut", self.name)] = 0
            elif self.id == 2:
                wire_lengths[utils.wire_name("cc1_sout", "cc2")] = (width_dict[self.name] + width_dict["carry_chain"])/4
                wire_layers[utils.wire_name("cc1_sout", "cc2")] = 0
                if self.updates == 3:
                    wire_lengths[utils.wire_name("cc1_sout", "reg4_sel")] = width_dict[self.name]
                    wire_layers[utils.wire_name("cc1_sout", "reg4_sel")] = 0


    def print_details(self):
        print " Carry Chain DETAILS:"

          
class _CarryChainSkipAnd(_SizableCircuit):
    """ Part of peripherals used in carry chain class.    """
    def __init__(self, use_finfet, use_tgate, carry_chain_type, N, FAs_per_flut, skip_size):
        # Carry chain name
        self.name = "xcarry_chain_and"
        self.use_finfet = use_finfet
        self.use_tgate = use_tgate
        # ripple or skip?
        self.carry_chain_type = carry_chain_type
        assert self.carry_chain_type == "skip"
        # size of the skip
        self.skip_size = skip_size
        # 1 FA per FA or 2?
        self.FAs_per_flut = FAs_per_flut
        # how many Fluts do we have in a cluster?
        self.N = N

        self.nand1_size = 2
        self.nand2_size = 2

        # this size is currently a limit due to how the and tree is being generated
        assert skip_size >= 4 and skip_size <=9

        if skip_size == 6:
            self.nand2_size = 3
        elif skip_size == 5:
            self.nand1_size = 3
        elif skip_size > 6:
            self.nand1_size = 3
            self.nand2_size = 3


    def generate(self, subcircuit_filename):
        """ Generate Carry chain SPICE netlists."""  

        self.transistor_names, self.wire_names = lut_subcircuits.generate_skip_and_tree(subcircuit_filename, self.name, self.use_finfet, self.nand1_size, self.nand2_size)

        self.initial_transistor_sizes["inv_nand"+str(self.nand1_size)+"_xcarry_chain_and_1_nmos"] = 1
        self.initial_transistor_sizes["inv_nand"+str(self.nand1_size)+"_xcarry_chain_and_1_pmos"] = 1
        self.initial_transistor_sizes["inv_xcarry_chain_and_2_nmos"] = 1
        self.initial_transistor_sizes["inv_xcarry_chain_and_2_pmos"] = 1
        self.initial_transistor_sizes["inv_nand"+str(self.nand2_size)+"_xcarry_chain_and_3_nmos"] = 1
        self.initial_transistor_sizes["inv_nand"+str(self.nand2_size)+"_xcarry_chain_and_3_pmos"] = 1
        self.initial_transistor_sizes["inv_xcarry_chain_and_4_nmos"] = 1
        self.initial_transistor_sizes["inv_xcarry_chain_and_4_pmos"] = 1

        return self.initial_transistor_sizes

    def generate_top(self):
        """ Generate Top-level Evaluation Path for Carry chain """

        self.top_spice_path = top_level.generate_carrychainand_top(self.name, self.use_tgate, self.nand1_size, self.nand2_size)

    def update_area(self, area_dict, width_dict):
        """ Calculate Carry Chain area and update dictionaries. """
        area_1 = (area_dict["inv_nand"+str(self.nand1_size)+"_xcarry_chain_and_1"] + area_dict["inv_xcarry_chain_and_2"])* int(math.ceil(float(self.skip_size/self.nand1_size)))
        area_2 = area_dict["inv_nand"+str(self.nand2_size)+"_xcarry_chain_and_3"] + area_dict["inv_xcarry_chain_and_4"]
        area = area_1 + area_2
        area_with_sram = area
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        if self.FAs_per_flut ==2:
            wire_lengths["wire_" + self.name + "_1"] = (width_dict["ble"]*self.skip_size)/4.0
        else:
            wire_lengths["wire_" + self.name + "_1"] = (width_dict["ble"]*self.skip_size)/2.0
        wire_layers["wire_" + self.name + "_1"] = 0
        wire_lengths["wire_" + self.name + "_2"] = width_dict[self.name]/2.0
        wire_layers["wire_" + self.name + "_2"] = 0

    def print_details(self):
        print " Carry Chain DETAILS:"


class _CarryChainInterCluster(_SizableCircuit):

    number = 1
    """ Wire dirvers of carry chain path between clusters"""
    def __init__(self, use_finfet, carry_chain_type, inter_wire_length, updates = 0):
        # Call the constructor of the base class
        _SizableCircuit.__init__(self, "carry_chain_inter", False, use_finfet)
        # Ripple or Skip?
        self.carry_chain_type = carry_chain_type
        # length of the wire between cout of a cluster to cin of the other
        self.inter_wire_length = inter_wire_length
        # Instance id
        self.id = _CarryChainInterCluster.number
        # if this is update 2 and this is the second adder add 2 to its name
        if updates and self.id == 2:
            self.name += str(self.id)
        # increment the static variable with 1 when creating a new instance
        _CarryChainInterCluster.number += 1


    def generate(self, subcircuit_filename):
        """ Generate Carry chain SPICE netlists."""  
        print("Generating " + self.name)

        self.transistor_names, self.wire_names = lut_subcircuits.generate_carry_inter(subcircuit_filename, self.name)

        self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 2
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 2

        return self.initial_transistor_sizes

    def generate_top(self):
        """ Generate Top-level Evaluation Path for Carry chain """
        print("Generating top-level " + self.name)
        self.top_spice_path = top_level.generate_carry_inter_top(self.name, self.id)

    def update_area(self, area_dict, width_dict):
        """ Calculate Carry Chain area and update dictionaries. """
        area = area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]
        area_with_sram = area
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        wire_lengths["wire_" + self.name + "_1"] = width_dict["tile"] * self.inter_wire_length
        wire_layers["wire_" + self.name + "_1"] = 0



class _CarryChainSkipMux(_SizableCircuit):
    """ Part of peripherals used in carry chain class.    """
    def __init__(self, use_finfet, carry_chain_type, use_tgate):
        # Carry chain name
        self.name = "xcarry_chain_mux"
        self.use_finfet = use_finfet
        # ripple or skip?
        self.carry_chain_type = carry_chain_type
        assert self.carry_chain_type == "skip"
        self.use_tgate = use_tgate



    def generate(self, subcircuit_filename):
        """ Generate the SPICE netlists."""  

        if not self.use_tgate :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2_to_1_mux(subcircuit_filename, self.name)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_nmos"] = 2
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 5
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 5
        else :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2_to_1_mux(subcircuit_filename, self.name)      
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 5
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 5
       
        return self.initial_transistor_sizes

    def generate_top(self):       

        print "Generating top-level " + self.name
        self.top_spice_path = top_level.generate_skip_mux_top(self.name, self.use_tgate)

    def update_area(self, area_dict, width_dict):

        if not self.use_tgate :
            area = (2*area_dict["ptran_" + self.name] +
                    area_dict["rest_" + self.name] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])
        else :
            area = (2*area_dict["tgate_" + self.name] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])

        area = area + area_dict["sram"]
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width

        return area
                

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire of member objects. """
        # Update wire lengths
        if not self.use_tgate :
            wire_lengths["wire_" + self.name] = width_dict["ptran_" + self.name]
        else :
            wire_lengths["wire_" + self.name] = width_dict["tgate_" + self.name]

        wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_1"])/4
        
        # Update wire layers
        wire_layers["wire_" + self.name] = 0
        wire_layers["wire_lut_to_flut_mux"] = 0
        wire_layers["wire_" + self.name + "_driver"] = 0 


class _FlipFlop(_CompoundCircuit):
    """ FlipFlop class.
        COFFE does not do transistor sizing for the flip flop. Therefore, the FF is not a SizableCircuit.
        Regardless of that, COFFE has a FlipFlop object that is used to obtain FF area and delay.
        COFFE creates a SPICE netlist for the FF. The 'initial_transistor_sizes', defined below, are
        used when COFFE measures T_setup and T_clock_to_Q. Those transistor sizes were obtained
        through manual design for PTM 22nm process technology. If you use a different process technology,
        you may need to re-size the FF transistors. """

    ff_generated = False
    
    def __init__(self, Rsel, use_tgate, use_finfet, input_size = 2, updates = 0):
        # Call the constructor of the base class
        _CompoundCircuit.__init__(self, "ff", use_tgate, use_finfet)
        # Register select mux, Rsel = LUT input (e.g. 'a', 'b', etc.) or 'z' if no register select 
        self.register_select = Rsel
        # A list of the names of transistors in this subcircuit.
        self.transistor_names = []
        # A list of the names of wires in this subcircuit
        self.wire_names = []
        # A dictionary of the initial transistor sizes
        self.initial_transistor_sizes = {}
        # Path to the top level spice file
        self.top_spice_path = ""    
        # 
        self.t_setup = 1
        # 
        self.t_clk_to_q = 1
        # Delay weight used to calculate delay of representative critical path
        self.delay_weight = DELAY_WEIGHT_FF

        self.input_size = input_size
        self.updates = updates
        self.MUX_NAME = ""

        if updates:
            self.name += str(input_size)

        if Rsel == 'z':
            self.Rsel = False
        else:
            self.Rsel = True
            # this shouldn't be changes as it's used with that name in the ff subcircuit created 
            # in the ff_subcircuit.py file
            self.MUX_NAME = self.name + "_input_select"
            # we need a mux without driver since the ff has its own dirver already
            with_driver = False
            FF = True
            self.input_mux = _MUX(self.MUX_NAME, use_tgate, input_size, with_driver, FF)

         
    def generate(self, subcircuit_filename):
        """ Generate FF SPICE netlists. Optionally includes register select. """
        
        # Generate FF with optional register select
        if self.Rsel:
            print("Generating FF (" + self.name + ")" + " with register select on BLE input " + self.register_select)
        else:
            print("Generating FF (" + self.name + ")") 
        self.transistor_names, self.wire_names = ff_subcircuits.generate_dff(subcircuit_filename, self.name, 
                                self.use_finfet, self.use_tgate, self.Rsel, self.MUX_NAME, _FlipFlop.ff_generated)    
        
        # TODO: there is a tiny changes in values when uncommenting this and commenting the above one
        # couldn't figure out why. This  chage is only in the dummy_lut6 test!
        
        # Generate FF with optional register select
        #if self.register_select == 'z':
        #    print "Generating FF"
        #    if not self.use_tgate:
        #        self.transistor_names, self.wire_names = ff_subcircuits.generate_ptran_d_ff(subcircuit_filename, self.use_finfet)
        #    else :
        #        self.transistor_names, self.wire_names = ff_subcircuits.generate_tgate_d_ff(subcircuit_filename, self.use_finfet)
        #else:
        #    print "Generating FF with register select on BLE input " + self.register_select
        #    if not self.use_tgate:
        #        self.transistor_names, self.wire_names = ff_subcircuits.generate_ptran_2_input_select_d_ff(subcircuit_filename, self.use_finfet)
        #    else :
        #        self.transistor_names, self.wire_names = ff_subcircuits.generate_tgate_2_input_select_d_ff(subcircuit_filename, self.use_finfet)
        

        # Give initial transistor sizes
        if self.Rsel:
            self.initial_transistor_sizes.update(self.input_mux.generate(subcircuit_filename))
            #self.transistor_names.extend(self.input_mux.transistor_names)
            #self.wire_names.extend(self.input_mux.wire_names)

            # Overwrite the initial transistor sizes for the mux
            if not self.use_tgate:
                self.initial_transistor_sizes["ptran_"+self.MUX_NAME+"_nmos"] = 4
                self.initial_transistor_sizes["rest_"+self.MUX_NAME+"_pmos"] = 1
            else:
                self.initial_transistor_sizes["tgate_"+self.MUX_NAME+"_nmos"] = 4
                self.initial_transistor_sizes["tgate_"+self.MUX_NAME+"_pmos"] = 4
        
        # These transistors always exists regardless of register select
        if not _FlipFlop.ff_generated: 
            if not self.use_finfet:
                self.initial_transistor_sizes["inv_ff_input_1_nmos"] = 3
                self.initial_transistor_sizes["inv_ff_input_1_pmos"] = 8.2
                self.initial_transistor_sizes["tgate_ff_1_nmos"] = 1
                self.initial_transistor_sizes["tgate_ff_1_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_set_n_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_reset_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc1_1_nmos"] = 3
                self.initial_transistor_sizes["inv_ff_cc1_1_pmos"] = 4
                self.initial_transistor_sizes["inv_ff_cc1_2_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc1_2_pmos"] = 1.3
                self.initial_transistor_sizes["tgate_ff_2_nmos"] = 1
                self.initial_transistor_sizes["tgate_ff_2_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_reset_n_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_set_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc2_1_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc2_1_pmos"] = 1.3
                self.initial_transistor_sizes["inv_ff_cc2_2_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc2_2_pmos"] = 1.3
                self.initial_transistor_sizes["inv_ff_output_driver_nmos"] = 4
                self.initial_transistor_sizes["inv_ff_output_driver_pmos"] = 9.7
            else :
                self.initial_transistor_sizes["inv_ff_input_1_nmos"] = 3
                self.initial_transistor_sizes["inv_ff_input_1_pmos"] = 9
                self.initial_transistor_sizes["tgate_ff_1_nmos"] = 1
                self.initial_transistor_sizes["tgate_ff_1_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_set_n_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_reset_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc1_1_nmos"] = 3
                self.initial_transistor_sizes["inv_ff_cc1_1_pmos"] = 4
                self.initial_transistor_sizes["inv_ff_cc1_2_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc1_2_pmos"] = 2
                self.initial_transistor_sizes["tgate_ff_2_nmos"] = 1
                self.initial_transistor_sizes["tgate_ff_2_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_reset_n_pmos"] = 1
                self.initial_transistor_sizes["tran_ff_set_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc2_1_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc2_1_pmos"] = 2
                self.initial_transistor_sizes["inv_ff_cc2_2_nmos"] = 1
                self.initial_transistor_sizes["inv_ff_cc2_2_pmos"] = 2
                self.initial_transistor_sizes["inv_ff_output_driver_nmos"] = 4
                self.initial_transistor_sizes["inv_ff_output_driver_pmos"] = 10

        _FlipFlop.ff_generated = True


        return self.initial_transistor_sizes


    def generate_top(self):
        """ """
        # TODO for T_setup and T_clock_to_Q
        if self.Rsel != 'z' and self.updates:
            self.top_spice_path = top_level.generate_ff_top(self.name, self.use_tgate, self.updates, self.input_size)
            self.input_mux.top_spice_path = self.top_spice_path
        

    def update_area(self, area_dict, width_dict):
        """ Calculate FF area and update dictionaries. """

        area = 0.0
        if self.Rsel:
           area = self.input_mux.update_area(area_dict, width_dict)
           if not self.use_tgate:
               area += area_dict["rest_" + self.MUX_NAME]

        # Add area of FF circuitry
        area += (area_dict["inv_ff_input_1"] +
                 area_dict["tgate_ff_1"] +
                 area_dict["tran_ff_set_n"] +
                 area_dict["tran_ff_reset"] +
                 area_dict["inv_ff_cc1_1"] +
                 area_dict["inv_ff_cc1_2"] +
                 area_dict["tgate_ff_2"] +
                 area_dict["tran_ff_reset_n"] +
                 area_dict["tran_ff_set"] +
                 area_dict["inv_ff_cc2_1"] +
                 area_dict["inv_ff_cc2_2"]+
                 area_dict["inv_ff_output_driver"])        

        # Add the SRAM bit if FF input select is on
        if self.Rsel:
            area += area_dict["sram"]
        
        # Calculate width and add to dictionaries
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width
        
        return area
        
        
    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio = 0):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        if self.Rsel:
            self.input_mux.update_wires(width_dict, wire_lengths, wire_layers)

        wire_lengths["wire_ff_input_out"] = (width_dict["inv_ff_input_1"] + width_dict["tgate_ff_1"])/4
        wire_lengths["wire_ff_tgate_1_out"] = (width_dict["tgate_ff_1"] + width_dict["inv_ff_cc1_1"])/4
        wire_lengths["wire_ff_cc1_out"] = (width_dict["inv_ff_cc1_1"] + width_dict["tgate_ff_2"])/4
        wire_lengths["wire_ff_tgate_2_out"] = (width_dict["tgate_ff_2"] + width_dict["inv_ff_cc1_2"])/4
        wire_lengths["wire_ff_cc2_out"] = (width_dict["inv_ff_cc1_2"] + width_dict["inv_ff_output_driver"])/4
    
        for wire in self.wire_names:
            wire_layers[wire] = 0
        
        
    def print_details(self):
        print "  FF DETAILS:"
        if self.register_select == 'z':
            print "  Register select: None"
        else:
            print "  Register select: BLE input " + self.register_select



        
class _LocalBLEOutput(_SizableCircuit):
    """ Local BLE Output is a 2:1 mux that feeds the output signal of an ALM back into the loca interconnect
        as feedback signal """
    
    def __init__(self, use_tgate, use_fluts):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "local_ble_output", use_tgate)

        # Delay weight in a representative critical path
        self.delay_weight = DELAY_WEIGHT_LOCAL_BLE_OUTPUT

        self.use_fluts = use_fluts
        
        
    def generate(self, subcircuit_filename):
        print "Generating local BLE output"
        if not self.use_tgate :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2_to_1_mux(subcircuit_filename, self.name)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_nmos"] = 2
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 4
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 4
        else :
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2_to_1_mux(subcircuit_filename, self.name)
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 4
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 4
      
        return self.initial_transistor_sizes


    def generate_top(self):
        print "Generating top-level " + self.name
        self.top_spice_path = top_level.generate_local_ble_output_top(self.name, self.use_tgate, self.use_fluts)
        
        
    def update_area(self, area_dict, width_dict):
        if not self.use_tgate :
            area = (2*area_dict["ptran_" + self.name] +
                    area_dict["rest_" + self.name] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])
        else :
            area = (2*area_dict["tgate_" + self.name] +
                    area_dict["inv_" + self.name + "_1"] +
                    area_dict["inv_" + self.name + "_2"])

        area = area + area_dict["sram"]
        width = math.sqrt(area)
        area_dict[self.name] = area
        width_dict[self.name] = width

        return area
        
    
    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio = 0):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
    
        # Update wire lengths
        if not self.use_tgate :
            wire_lengths["wire_" + self.name] = width_dict["ptran_" + self.name]
        else :
            wire_lengths["wire_" + self.name] = width_dict["tgate_" + self.name]

        wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_1"])/4
        
        # Update wire layers
        wire_layers["wire_" + self.name] = 0
        wire_layers["wire_" + self.name + "_driver"] = 0
        
        
    def print_details(self):
        print "Local BLE output details."

      
class _GeneralBLEOutput(_SizableCircuit):
    """ General BLE Output is the mux at the output of the ble connecting its output to the general routing """
    
    def __init__(self, use_tgate, use_fluts, enable_carry_chain, updates = 0, input_size = 2):
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "general_ble_output", use_tgate)

        self.delay_weight = DELAY_WEIGHT_GENERAL_BLE_OUTPUT
        self.use_fluts = use_fluts
        self.enable_carry_chain = enable_carry_chain

        # updates the local ble output class will support creating either a 2:1 mux or a 3:1 mux
        # it should be called twice in the ble class to create the two output mux types needed
        self.updates = updates
        self.input_size = input_size

        # for the new design we have a 2:1 and a 3:1 mux. So we need to add the input size to the mux
        # name to be able to differentiate them
        if self.updates and self.input_size == 3:
            self.name += str(input_size)
        
        
    def generate(self, subcircuit_filename):
        print "Generating general BLE output"

        # if the mux is a 3:1 mux which should happen only for the new design, create the mux using the 2lvl mux function
        # the mux will need one additional ptran or one additional transmission gate to its critical path
        if self.updates and self.input_size == 3:
            if not self.use_tgate:
                self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2lvl_mux(subcircuit_filename, self.name, 2, 2)
                self.initial_transistor_sizes["ptran_" + self.name + "_L1_nmos"] = 2
                self.initial_transistor_sizes["ptran_" + self.name + "_L2_nmos"] = 2
                self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            else: 
                self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2lvl_mux(subcircuit_filename, self.name, 2, 2)
                self.initial_transistor_sizes["tgate_" + self.name + "_L1_nmos"] = 2
                self.initial_transistor_sizes["tgate_" + self.name + "_L1_pmos"] = 2
                self.initial_transistor_sizes["tgate_" + self.name + "_L2_nmos"] = 2
                self.initial_transistor_sizes["tgate_" + self.name + "_L2_pmos"] = 2
        else:
            if not self.use_tgate:
                self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2_to_1_mux(subcircuit_filename, self.name)
                self.initial_transistor_sizes["ptran_" + self.name + "_nmos"] = 2
                self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            else:
                self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2_to_1_mux(subcircuit_filename, self.name)
                self.initial_transistor_sizes["tgate_" + self.name + "_nmos"] = 2
                self.initial_transistor_sizes["tgate_" + self.name + "_pmos"] = 2

        self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 5
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 5

        return self.initial_transistor_sizes


    def generate_top(self):
        print "Generating top-level " + self.name
        self.top_spice_path = top_level.generate_general_ble_output_top(self.name, self.use_tgate, self.use_fluts, 
                                                                        self.updates, self.input_size)
        if self.updates in (1, 2, 3, 10) and self.input_size == 2:
            self.wire_names.append(utils.wire_name("fmux_l1_duplicate", "ff"))
            self.wire_names.append(utils.wire_name("ffin", "gbo2"))
        elif self.updates == 4 and self.input_size == 2:
            self.wire_names.append(utils.wire_name("fmux_l2_duplicate", "ff2"))
        if self.updates and self.input_size == 2:
            self.wire_names.append(utils.wire_name("ff2_mux", "gbo2"))


    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio = 0):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        level = ""
        switch = "ptran_"
        if self.updates and self.input_size == 3:
            level = "_L1"
        if self.use_tgate:
            switch = "tgate_"
    
        # Update wire lengths
        wire_lengths["wire_" + self.name + level] = width_dict[switch + self.name + level]
        wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_1"])/4

        if self.input_size == 2:
            if self.updates in (1, 2, 3, 10):
                # TODO: revise all these lengths
                wire_lengths[utils.wire_name("fmux_l1_duplicate", "ff")] = width_dict["ff"]*2
                wire_lengths[utils.wire_name("ffin", "gbo2")] = width_dict["ff"]
                wire_lengths[utils.wire_name("ff2_mux", "gbo2")] = width_dict["ff"]
            # wire connecting fmux_l2 duplicate with the input select mux in front of the ff
            elif self.updates == 4:
                wire_lengths[utils.wire_name("fmux_l2_duplicate", "ff2")] = (width_dict["fmux_l2"] + width_dict["ff2_input_select"])/4
                wire_lengths[utils.wire_name("ff2_mux", "gbo2")] = width_dict["ff"]
 
        

        if self.updates and self.input_size == 3:
            wire_lengths["wire_" + self.name + "_L2"] = (width_dict[switch + self.name + level] + width_dict[switch + self.name + "_L2"])/4
            #wire_layers["wire_" + self.name + "_L2"] = 0

        for wire in self.wire_names:
            wire_layers[wire] = 0


    def update_area(self, area_dict, width_dict):

        if not self.use_tgate:
            if not self.updates or self.input_size == 2:
                area = (2*area_dict["ptran_" + self.name] +
                        area_dict["rest_" + self.name])                    
            else:
                area = (4*area_dict["ptran_" + self.name + "_L1"] + 
                        2*area_dict["ptran_" + self.name + "_L2"] +
                          area_dict["rest_" + self.name])
        else:
            if not self.updates or self.input_size == 2:
                area =  2*area_dict["tgate_" + self.name]
            else:
                area = (4*area_dict["tgate_" + self.name + "_L1"] + 
                        2*area_dict["tgate_" + self.name + "_L2"])
                          
        # all types of general ble output muxes have those two inverters
        area += area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]

        # MUX area including SRAM
        if self.updates and self.input_size == 3:
            area_with_sram = area + 4*area_dict["sram"]  
            width = math.sqrt(area)
            width_with_sram = math.sqrt(area_with_sram)
            area_dict[self.name] = area
            width_dict[self.name] = width
            area_dict[self.name + "_sram"] = area_with_sram
            width_dict[self.name + "_sram"] = width_with_sram
        else:
            area = area + area_dict["sram"]
            width = math.sqrt(area)
            area_dict[self.name] = area
            width_dict[self.name] = width

        return area
        
   
    def print_details(self):
        print("General BLE output details.")

        
class _LUTOutputLoad:
    """ LUT output load is the load seen by the output of the LUT in the basic case if Or = 1 and Ofb = 1 (see [1])
        then the output load will be the regster select mux of the flip-flop, the mux connecting the output signal
        to the output routing and the mux connecting the output signal to the feedback mux """

    def __init__(self, num_local_outputs, num_general_outputs, use_fluts, enable_carry_chain, updates):
        self.name = "lut_output_load"

        # A list of the names of wires in this subcircuit
        self.wire_names = []

        self.num_local_outputs = num_local_outputs
        self.num_general_outputs = num_general_outputs

        self.use_fluts = use_fluts
        self.enable_carry_chain = enable_carry_chain
        self.updates = updates
        if self.updates:
            self.name = "flut_output_load"
        
        
    def generate(self, subcircuit_filename):
        print "Generating LUT output load"

        if not self.updates:
            self.wire_names = load_subcircuits.generate_lut_output_load(subcircuit_filename)

        if self.use_fluts:
            self.wire_names.extend(load_subcircuits.generate_flut_output_load(subcircuit_filename, self.enable_carry_chain, self.updates))    
        
     
    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        # TODO: the load is just a subcircuit that combines some other circuits together there shouldn't be
        # any wire definitions here. I think it would be better if the wire definitions are moved to the 
        # subcircuits objects
        if self.use_fluts:
            if not self.updates:
                wire_lengths[utils.wire_name("lut", "flut_mux")] = width_dict["lut"]/2 * lut_ratio
            elif self.updates:
                wire_lengths[utils.wire_name("lut", "fmux_l1")] = width_dict["lut"]/2 * lut_ratio
                if self.updates in (1, 2, 3, 10):
                    wire_lengths[utils.wire_name("lut", "fmux_l1_duplicate")] = width_dict["lut"]/2 * lut_ratio
            if self.enable_carry_chain:
                #JUNIUS - calculate wire length for mode 10 (LUT skip)
                if self.updates == 10:
                    wire_lengths[utils.wire_name("lut", "flut_cc_mux")] = width_dict["lut"]/2 * lut_ratio - width_dict["flut_cc_mux"]
                else:
                    wire_lengths[utils.wire_name("lut", "carry_chain")] = width_dict["lut"]/2 * lut_ratio
        
        if not self.updates:
            # Update wire lengths
            wire_lengths["wire_lut_output_load_1"] = (width_dict["ff"] + width_dict["lut_and_drivers"])/8
            wire_lengths["wire_lut_output_load_2"] = width_dict["ff"]
            
        # update wire layers
        for wire in self.wire_names:
            wire_layers[wire] = 0

#JUNIUS - add carry chain MUX at LUT output for LUT skipping (mode 10)
class _flut_cc_mux(_SizableCircuit):
    """
    Used in updates mode 10, to skip the LUTs and connect directly to the adder-direct local input crossbar.
    """
    def __init__(self, use_tgate=False, use_finfet=False):
        _SizableCircuit.__init__(self, "flut_cc_mux", use_tgate, use_finfet)
        self.name = "flut_cc_mux"
        self.mux = _MUX(self.name, self.use_tgate)

    def generate(self, subcircuit_filename):
        """
        This function:
        - Generates the 2:1 MUX
        - Gets all transistor sizing and wire names needed for the MUX
        - Adds a wire that feeds the mux the list of wire names
        """
        print("Generating " + self.name)

        self.initial_transistor_sizes = self.mux.generate(subcircuit_filename)
        self.transistor_names = self.mux.transistor_names
        self.wire_names = self.mux.wire_names
        self.wire_names.extend(load_subcircuits.generate_flut_cc_mux_output_load(subcircuit_filename))

        return self.initial_transistor_sizes
    
    def generate_top(self):
        print "Generating top-level " + self.name
        self.top_spice_path = top_level.generate_flut_cc_mux_top(self.name, self.use_tgate)
    
    def update_area(self, area_dict, width_dict):
        return self.mux.update_area(area_dict, width_dict)
    
    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio):
        self.mux.update_wires(width_dict, wire_lengths, wire_layers, lut_ratio)
        wire_lengths[utils.wire_name("flut_cc_mux", "carry_chain")] = width_dict[self.name] / 2

        # Update layer
        for name in self.wire_names:
            wire_layers[name] = 0

class _flut_mux(_SizableCircuit):
    """ TODO: add discreption"""
    
    def __init__(self, use_tgate, use_finfet, enable_carry_chain, updates = 0, level = 1):


        # TODO: change to enable finfet support, should be rather straightforward as it's just a mux
        # use finfet
        # TODO: unify the names of the flut_mux; make it always fmux_l#n for easier implementation
        # name
        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, "flut_mux", use_tgate, use_finfet)

        self.enable_carry_chain = enable_carry_chain
        #add the level of the flut to its name and define some new variables
        self.updates = updates
        # fmux level either 1 or 2
        self.level = level

        if updates:
            self.name = "fmux_l" + str(level)

        self.mux = _MUX(self.name, self.use_tgate)


    def generate(self, subcircuit_filename):
        """ This function generates the flut 2:1 mux gets all the transistor sizing and 
            the wire names needed for the mux and adds a wire that feeds the mux the 
            list of wire names """
        print "Generating flut added mux"   

        self.initial_transistor_sizes = self.mux.generate(subcircuit_filename)
        self.transistor_names = self.mux.transistor_names
        self.wire_names = self.mux.wire_names

        # For Stratix10: generate the output loading of fmux2
        # For Stratix10 level3: generate the output loading of fmux1 and fmux3
        if (self.updates in (1, 2, 3, 10) and self.level == 2) or (self.updates == 4 and self.level == 3):
            self.wire_names.extend(load_subcircuits.generate_last_fmux_output_load(subcircuit_filename, self.level, self.updates))
        # generates a load containing the fmux_l2 and fmux_l2_duplicate and resturns the wire connecting them
        elif self.updates == 4 and self.level == 1:
            self.wire_names.extend(load_subcircuits.generate_fmux_l1_output_load(subcircuit_filename))

        return self.initial_transistor_sizes


    def generate_top(self):       

        print "Generating top-level " + self.name
        # TODO: why not just feed the instance to it?
        self.top_spice_path = top_level.generate_flut_mux_top(self.name, self.use_tgate, self.enable_carry_chain, self.level, 
                                                              self.updates)

        # Note: those two wires are the only two wire not created already 
        # in the load circuites created in the generate function

        # add wire connecting the flut output load to the flut mux level2
        if self.updates in (1, 2, 3, 10) and self.level == 1:
            self.wire_names.append(utils.wire_name("fmux_l1", "fmux_l2"))

        # add the wire connecting the flut mux l2 and flut mux l3
        if self.updates == 4 and self.level == 2:
            self.wire_names.append(utils.wire_name("fmux_l2", "fmux_l3"))
            

    def update_area(self, area_dict, width_dict):
        """ updates the area according to the current transistor areas 
            since this flut mux only has one mux this translates to updating
            the area of the mux object"""

        return self.mux.update_area(area_dict, width_dict)
                

    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio):
        """ Update wire of member objects. """
        
        # updates wires in the mux object
        self.mux.update_wires(width_dict, wire_lengths, wire_layers, lut_ratio)

        # current fmux and next fmux names
        fmux1 = "fmux_l"+str(self.level)
        fmux2 = "fmux_l"+str(self.level+1)

        # For Stratix10 or Stratix10 level3: wire connecting fmux_l1 and fmux_l2
        # For Stratix10 level 3: there is an extra wire from fmux_l2 to fmux_l3
        if self.updates:
            if self.level == 1 or (self.updates == 4 and self.level == 2):
                wire_lengths[utils.wire_name(fmux1, fmux2)] = (width_dict[fmux1] + width_dict[fmux2])/4

        # wire connecting fmux_l1 to fmux_l2 duplicate
        if self.updates == 4 and self.level == 1:
            wire_lengths[utils.wire_name(fmux1, fmux2+"_duplicate")] = (width_dict[fmux1] + width_dict[fmux2])/4

        # two wires connecting the last level fmux and the 3:1 general ble output mux
        # and the input of the 3:1 register input select mux
        # TODO: update those wire lengths
        if (self.updates in (1, 2, 3, 10) and self.level == 2) or self.level == 3:
            wire_lengths[utils.wire_name(fmux1, "gbo3")] = 2*width_dict['ff']
            wire_lengths[utils.wire_name(fmux1, "ff3")] = width_dict['ff']

        # Update wire layers
        for wire in self.wire_names:
            wire_layers[wire] = 0


class _MUX(_SizableCircuit):
    """ This is a mux class it could be used to create any type of mux needed """
    
    def __init__(self, name, use_tgate, input_size = 2, with_driver = True, FF = False, loads = False):

        # Call the constructor of the base class to initialize all
        # its local variables
        _SizableCircuit.__init__(self, name, use_tgate)

        #add the level of the flut to its name and define some new variables
        self.input_size = input_size
        self.loads = loads
        self.with_driver = with_driver
        # for a 3:1 mux the 2lvl mux function generates a mulfunctioning circuit
        # so this is a temporary hack for this problem
        if input_size == 3:
            self.input_size = 4
        self.level2_size = int(math.sqrt(self.input_size))
        self.level1_size = int(math.ceil(float(self.input_size)/self.level2_size))
        self.implemented_size = self.level1_size*self.level2_size
        self.num_unused_inputs = self.implemented_size - self.input_size
        self.sram_per_mux = self.level1_size + self.level2_size
        self.FF = FF

        if use_tgate:
            self.switch = "tgate_"
        else:
            self.switch = "ptran_"


    def generate(self, subcircuit_filename):
        """ This function generates the subcircuit needed for the mux. If the mux is a 
            2:1 it is implemented as a one level mux else it will be implemented as a 
            2 level mux """

        print "Generating " + self.name 

        # Create the mux subcircuits
        self.transistor_names, self.wire_names = mux_subcircuits.generate_mux(subcircuit_filename, self.name, 
                                                self.use_tgate, self.input_size, self.with_driver, self.FF, self.loads)
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        if self.input_size == 2:
            if not self.use_tgate:
                self.initial_transistor_sizes["ptran_" + self.name + "_nmos"] = 2
                if self.with_driver:
                    self.initial_transistor_sizes["rest_"  + self.name + "_pmos"] = 1
            else:
                self.initial_transistor_sizes["tgate_" + self.name + "_nmos"] = 2
                self.initial_transistor_sizes["tgate_" + self.name + "_pmos"] = 2
        elif self.input_size > 2:
            if not self.use_tgate:
                self.initial_transistor_sizes["ptran_" + self.name + "_L1_nmos"] = 3
                self.initial_transistor_sizes["ptran_" + self.name + "_L2_nmos"] = 4
                if self.with_driver:
                    self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            else:
                self.initial_transistor_sizes["tgate_" + self.name + "_L1_nmos"] = 3
                self.initial_transistor_sizes["tgate_" + self.name + "_L1_pmos"] = 3
                self.initial_transistor_sizes["tgate_" + self.name + "_L2_nmos"] = 4
                self.initial_transistor_sizes["tgate_" + self.name + "_L2_pmos"] = 4
        
        if self.with_driver:
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 5
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 5

        return self.initial_transistor_sizes


    def generate_top(self):       
        pass

    def update_area(self, area_dict, width_dict):
        """ updates the area according to the current transistor areas """
        
        if self.input_size == 2:
            area = 2*area_dict[self.switch + self.name]
        elif self.input_size > 2:
            area = (self.implemented_size*area_dict[self.switch + self.name + "_L1"] + 
                         self.level2_size*area_dict[self.switch + self.name + "_L2"])

        if self.with_driver:
             area += area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]
             if not self.use_tgate:
                area += area_dict["rest_" + self.name]

        # MUX area including SRAM
        if self.input_size == 2:
            area_with_sram = area + area_dict["sram"]
        elif self.input_size > 2:
            area_with_sram = area + (self.level1_size + self.level2_size)*area_dict["sram"]
        
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

        return area
                

    def update_wires(self, width_dict, wire_lengths, wire_layers, ratio = 1.0):
        """ Update wire of member objects. """

        # Update wire lengths
        if self.input_size == 2:
            wire_lengths["wire_" + self.name] = width_dict[self.switch + self.name]
        elif self.input_size > 2:
            wire_lengths["wire_" + self.name + "_L1"] = width_dict[self.name] * ratio
            wire_lengths["wire_" + self.name + "_L2"] = width_dict[self.name] * ratio

        # the wire_(self.name)_driver is the wire between the sense inveter and the driver inverter in a mux
        # it get the length of distance from the midpoint of the first to second.
        if self.with_driver:
            wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_1"])/4
        
        # Update wire layers
        for wire in self.wire_names:
            wire_layers[wire] = 0


class _BLE(_CompoundCircuit):

    def __init__(self, specs, carry_skip_periphery_count):
        # Calling the constructor of the base class
        _CompoundCircuit.__init__(self, "ble", specs.use_tgate, specs.use_finfet)
        # number of bles in a cluster
        self.N = specs.N
        # Size of LUT
        self.K = specs.K
        # Number of inputs to the BLE
        self.num_inputs = specs.K
        # Number of local outputs
        self.num_local_outputs = specs.num_ble_local_outputs
        # Number of general outputs
        self.num_general_outputs = specs.num_ble_general_outputs
        # TODO: why is the carry chain object not defined here?
        self.enable_carry_chain = specs.enable_carry_chain
        # Number of FAs in an BLE
        self.FAs_per_flut = specs.FAs_per_flut
        # Carry skip periphery count
        self.carry_skip_periphery_count = carry_skip_periphery_count
        # Boolean indicating that we are applying the new architecture
        self.updates = specs.updates
        # Are the LUTs fracturable?
        self.use_fluts = specs.use_fluts

        # Create BLE general output object
        self.general_output = _GeneralBLEOutput(specs.use_tgate, specs.use_fluts, specs.enable_carry_chain, specs.updates)
        self.subcircuits[self.general_output.name] = self.general_output

        # for the new architecture we need another general ble output mux which is a 3:1 mux
        if specs.updates:
            self.general_output3 = _GeneralBLEOutput(specs.use_tgate, specs.use_fluts, specs.enable_carry_chain, specs.updates, 3)
            self.subcircuits[self.general_output3.name] = self.general_output3

        # Create BLE local output object
        if not specs.updates:
            self.local_output = _LocalBLEOutput(specs.use_tgate, specs.use_fluts)
            self.subcircuits[self.local_output] = self.local_output

         # The extra mux for the fracturable luts
        if specs.use_fluts:
            self.fmux = _flut_mux(specs.use_tgate, specs.use_finfet, specs.enable_carry_chain, specs.updates, 1)
            self.subcircuits[self.fmux.name] = self.fmux

        # Stratix10 has two levels of flut mux
        if specs.updates:
            self.fmux_l2 = _flut_mux(specs.use_tgate, specs.use_finfet, specs.enable_carry_chain, specs.updates, 2)
            self.subcircuits[self.fmux_l2.name] = self.fmux_l2

        # Stratix10 level 3 have a third level of flut mux
        if specs.updates == 4:
            self.fmux_l3 = _flut_mux(specs.use_tgate, specs.use_finfet, specs.enable_carry_chain, specs.updates, 3)
            self.subcircuits[self.fmux_l3.name] = self.fmux_l3

        #JUNIUS - add CC MUX for LUT skip (mode 10)
        if specs.updates == 10:
            self.flut_cc_mux = _flut_cc_mux(specs.use_tgate, specs.use_finfet)
            self.subcircuits[self.flut_cc_mux.name] = self.flut_cc_mux

        # Create LUT object
        self.lut = _LUT(specs.K, specs.Rsel, specs.Rfb, specs.use_tgate, specs.use_finfet, specs.use_fluts, specs.min_tran_width, specs.updates)
        self.subcircuits[self.lut.name] = self.lut

        # Create FF object
        if not specs.updates:
            self.ff = _FlipFlop(specs.Rsel, specs.use_tgate, specs.use_finfet)
            self.subcircuits[self.ff.name] = self.ff

        # Stratix10 has 3 different types of FF, a FF without an input select mux (self.ff)
        # a FF with a 2:1 input select mux (self.ff2), and a FF with 3:1 input select mux (self.ff3)
        if specs.updates:
            # Note: ff is not needed for updates 4 however, it's just generated to take the area size of the ff from it
            self.ff  = _FlipFlop('z', specs.use_tgate, specs.use_finfet)
            self.subcircuits[self.ff.name] = self.ff
            self.ff3 = _FlipFlop('b', specs.use_tgate, specs.use_finfet, 3, specs.updates)
            self.subcircuits[self.ff3.name] = self.ff3
        if specs.updates in (1, 2, 4, 10):
            self.ff2 = _FlipFlop('a', specs.use_tgate, specs.use_finfet, 2, specs.updates)
            self.subcircuits[self.ff2.name] = self.ff2
        if specs.updates == 3:
            self.ff4 = _FlipFlop('a', specs.use_tgate, specs.use_finfet, 4, specs.updates)
            self.subcircuits[self.ff4.name] = self.ff4

        # Create LUT output load object
        self.lut_output_load = _LUTOutputLoad(self.num_local_outputs, self.num_general_outputs, self.use_fluts,
                                              self.enable_carry_chain, self.updates)
        self.loads[self.lut_output_load.name] = self.lut_output_load

        
        
    def generate(self, subcircuit_filename):
        print "Generating BLE"
        
        # Generate LUT and FF
        init_tran_sizes = {}

        # Generate all the subciruits
        for subcircuit in self.subcircuits.values():
            init_tran_sizes.update(subcircuit.generate(subcircuit_filename))

        # Generate ble outputs load which
        load_subcircuits.generate_ble_outputs(subcircuit_filename, self.num_local_outputs, self.num_general_outputs)

        # Generate LUT load
        self.lut_output_load.generate(subcircuit_filename)
       
        return init_tran_sizes

     
    def generate_top(self):
        """ Generate all the SPICE top files for all the subcircuits """
        for subcircuit in self.subcircuits.values():
            subcircuit.generate_top()
    
    def update_area(self, area_dict, width_dict):
        """ Update the areas for all the subcircuits """
        areas = {}

        # Update the areas for all the subcircuits
        for subcircuit in self.subcircuits.values():
            areas[subcircuit.name] = subcircuit.update_area(area_dict, width_dict)

        # new design doesn't have a local feedback mux
        if not self.updates:
            local_ble_output_area = self.num_local_outputs * areas[self.local_output.name]
        else:
            local_ble_output_area = 0

        if not self.updates:
            general_ble_output_area = self.num_general_outputs * areas[self.general_output.name]
        else:
            general_ble_output_area = 3 * areas[self.general_output.name] + areas[self.general_output3.name]


        ble_output_area = local_ble_output_area + general_ble_output_area
        ble_output_width = math.sqrt(ble_output_area)
        area_dict["ble_output"] = ble_output_area
        width_dict["ble_output"] = ble_output_width

        if self.updates == 4:
            ble_area = areas[self.lut.name] + 3 * areas[self.ff2.name] + areas[self.ff3.name] + ble_output_area
        elif self.updates == 3:
            ble_area = areas[self.lut.name] + 2 * areas[self.ff.name] + areas[self.ff3.name] + areas[self.ff4.name] + ble_output_area
        elif self.updates:
            ble_area = areas[self.lut.name] + 2 * areas[self.ff.name] + areas[self.ff2.name] + areas[self.ff3.name] + ble_output_area
            #JUNIUS - add CC MUX areas
            if self.updates == 10:
                ble_area += areas[self.flut_cc_mux.name] * 4
        elif self.use_fluts:
            ble_area = areas[self.lut.name] + 2 * areas[self.ff.name] + ble_output_area
        else:
            ble_area = areas[self.lut.name] + areas[self.ff.name] + ble_output_area

        if not self.updates:
            if self.enable_carry_chain == 1:
                ble_area += self.FAs_per_flut * (area_dict["carry_chain"] + area_dict["carry_chain_mux"])
                if self.carry_skip_periphery_count:
                    ble_area += ((area_dict["xcarry_chain_and"] + area_dict["xcarry_chain_mux"]) * self.carry_skip_periphery_count)/self.N
        else:
            ble_area += area_dict["carry_chain"] * self.FAs_per_flut
            
            if self.updates in (2, 3):
                ble_area += area_dict["carry_chain2"] * self.FAs_per_flut


        ble_width = math.sqrt(ble_area)
        area_dict["ble"] = ble_area
        width_dict["ble"] = ble_width
        
        
    def update_wires(self, width_dict, wire_lengths, wire_layers, lut_ratio):
        """ Update wire of member objects. """
        
        # update wire lengths of all subcircuits
        for subcircuit in self.subcircuits.values():
            subcircuit.update_wires(width_dict, wire_lengths, wire_layers, lut_ratio)

        # Wire connecting all BLE output mux-inputs together, in the net list this wire is divided into different wires
        # depending on the number of muxes we have. The number of individual wires equals to the total number of muxes - 1
        if not self.updates:
            wire_lengths["wire_ble_outputs"] = self.num_local_outputs*width_dict[self.local_output.name] + self.num_general_outputs*width_dict[self.general_output.name]
            wire_layers["wire_ble_outputs"] = 0

        # Update LUT load wires
        self.lut_output_load.update_wires(width_dict, wire_lengths, wire_layers, lut_ratio)

        
    def print_details(self, report_file):
    
        self.lut.print_details(report_file)
        
        
class _LocalBLEOutputLoad:

    def __init__(self):
        self.name = "local_ble_output_load"
        
        
    def generate(self, subcircuit_filename):
        load_subcircuits.generate_local_ble_output_load(subcircuit_filename)
     
     
    def update_wires(self, width_dict, wire_lengths, wire_layers, ble_ic_dis):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # Update wire lengths
        wire_lengths["wire_local_ble_output_feedback"] = width_dict["logic_cluster"]
        if ble_ic_dis !=0:
            wire_lengths["wire_local_ble_output_feedback"] = ble_ic_dis
        # Update wire layers
        wire_layers["wire_local_ble_output_feedback"] = 0


        

class _GeneralBLEOutputLoad:
    """ Logic cluster output load (i.e. general BLE output load). 
        Made up of a wire loaded by SB muxes. """

    def __init__(self):
        # Subcircuit name
        self.name = "general_ble_output_load"
        # Assumed routing channel usage, we need this for load calculation 
        self.channel_usage_assumption = 0.5
        # Assumed number of 'on' SB muxes on cluster output, needed for load calculation
        self.num_sb_mux_on_assumption = 1
        # Number of 'partially on' SB muxes on cluster output (calculated in compute_load)
        self.num_sb_mux_partial = -1
        # Number of 'off' SB muxes on cluster output (calculated in compute_load)
        self.num_sb_mux_off = -1
        # List of wires in this subcircuit
        self.wire_names = []
        
        
    def generate(self, subcircuit_filename, specs, sb_mux):
        """ Compute cluster output load load and generate SPICE netlist. """
        
        self._compute_load(specs, sb_mux, self.channel_usage_assumption, self.num_sb_mux_on_assumption)
        self.wire_names = load_subcircuits.generate_general_ble_output_load(subcircuit_filename, self.num_sb_mux_off, self.num_sb_mux_partial, self.num_sb_mux_on_assumption)
        
        
    def update_wires(self, width_dict, wire_lengths, wire_layers, h_dist, height):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # The BLE output wire is the wire that allows a BLE output to reach routing wires in
        # the routing channels. This wire spans some fraction of a tile. We can set what that
        # fraction is with the output track-access span (track-access locality).
        wire_lengths["wire_general_ble_output"] = width_dict["tile"]*OUTPUT_TRACK_ACCESS_SPAN
        if height != 0.0:
            wire_lengths["wire_general_ble_output"] = (h_dist)

        # Update wire layers
        wire_layers["wire_general_ble_output"] = 0
      

    def print_details(self, report_file):
        """ Print cluster output load details """
        
        utils.print_and_write(report_file, "  CLUSTER OUTPUT LOAD DETAILS:")
        utils.print_and_write(report_file, "  Total number of SB inputs connected to cluster output: " + str(self.num_sb_mux_off + self.num_sb_mux_partial + self.num_sb_mux_on_assumption))
        utils.print_and_write(report_file, "  Number of 'on' SB MUXes (assumed): " + str(self.num_sb_mux_on_assumption))
        utils.print_and_write(report_file, "  Number of 'partial' SB MUXes: " + str(self.num_sb_mux_partial))
        utils.print_and_write(report_file, "  Number of 'off' SB MUXes: " + str(self.num_sb_mux_off))
        utils.print_and_write(report_file, "")
        
      
    def _compute_load(self, specs, sb_mux, channel_usage, sb_mux_on):
        """ Calculate how many on/partial/off switch block multiplexers are connected to each cluster output.
            Inputs are FPGA specs object, switch block mux object, assumed channel usage and assumed number of on muxes.
            The function will update the object's off & partial attributes."""
        
        # Size of second level of switch block mux, need this to figure out how many partially on muxes are connected
        sb_level2_size = sb_mux.level2_size
        
        # Total number of switch block multiplexers connected to cluster output
        total_load = int(specs.Fcout*specs.W)
        
        # Let's calculate how many partially on muxes are connected to each output
        # Based on our channel usage assumption, we can determine how many muxes are in use in a tile.
        # The number of used SB muxes equals the number of SB muxes per tile multiplied by the channel usage.
        used_sb_muxes_per_tile = int(channel_usage*sb_mux.num_per_tile)
        
        # Each one of these used muxes comes with a certain amount of partially on paths.
        # We calculate this based on the size of the 2nd muxing level of the switch block muxes
        total_partial_paths = used_sb_muxes_per_tile*(sb_level2_size-1)
        
        # The partially on paths are connected to both routing wires and cluster outputs
        # We assume that they are distributed evenly across both, which means we need to use the
        # ratio of sb_mux inputs coming from routing wires and coming from cluster outputs to determine
        # how many partially on paths would be connected to cluster outputs
        sb_inputs_from_cluster_outputs = total_load*specs.num_cluster_outputs
        # We use the required size here because we assume that extra inputs that may be present in the "implemented" mux
        # might be connected to GND or VDD and not to routing wires
        sb_inputs_from_routing = sb_mux.required_size*sb_mux.num_per_tile - sb_inputs_from_cluster_outputs
        frac_partial_paths_on_cluster_out = float(sb_inputs_from_cluster_outputs)/(sb_inputs_from_cluster_outputs+sb_inputs_from_routing)
        # The total number of partial paths on the cluster outputs is calculated using that fraction
        total_cluster_output_partial_paths = int(frac_partial_paths_on_cluster_out*total_partial_paths)
        # And we divide by the number of cluster outputs to get partial paths per output
        cluster_output_partial_paths = int(math.ceil(float(total_cluster_output_partial_paths)/specs.num_cluster_outputs))
        
        # Now assign these numbers to the object
        self.num_sb_mux_partial = cluster_output_partial_paths
        self.num_sb_mux_off = total_load - self.num_sb_mux_partial - sb_mux_on
    

class _LocalRoutingWireLoad:
    """ Local routing wire load """
    
    def __init__(self, I, N, K, num_ble_local_outputs, local_mux_l2_size, local_mux_implemented_size, name = "local_routing_wire_load"):
        # Name of this wire
        self.name = name
        # How many LUT inputs are we assuming are used in this logic cluster? (%)
        self.lut_input_usage_assumption = 0.85
        # Total number of local mux inputs per wire
        self.mux_inputs_per_wire = -1
        # Number of on inputs connected to each wire 
        self.on_inputs_per_wire = -1
        # Number of partially on inputs connected to each wire
        self.partial_inputs_per_wire = -1
        #Number of off inputs connected to each wire
        self.off_inputs_per_wire = -1
        # List of wire names in the SPICE circuit
        self.wire_names = []
        # Cluster input size
        self.I = I
        # Number of BLEs in the cluster
        self.N = N
        # LUT size
        self.K = K
        # number of ble feedback outputs
        self.num_ble_local_outputs = num_ble_local_outputs
        # Local routing mux instance
        self.local_mux_l2_size = local_mux_l2_size
        # Local mux implemented size
        self.local_mux_implemented_size = local_mux_implemented_size
    

    def generate(self, subcircuit_filename):
        print "Generating " + self.name
        # Compute load (number of on/partial/off per wire)
        self._compute_load()
        # Generate SPICE deck
        self.wire_names = load_subcircuits.local_routing_load_generate(subcircuit_filename, self.on_inputs_per_wire, self.partial_inputs_per_wire, self.off_inputs_per_wire, name=self.name)
    
    
    def update_wires(self, width_dict, wire_lengths, wire_layers, local_routing_wire_load_length):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # Update wire lengths
        wire_lengths["wire_" + self.name] = width_dict["logic_cluster"]
        if local_routing_wire_load_length !=0:
            wire_lengths["wire_" + self.name] = local_routing_wire_load_length
        # Update wire layers
        wire_layers["wire_" + self.name] = 0
    
        
    def print_details(self):
        print "LOCAL ROUTING WIRE LOAD DETAILS"
        print ""
        
        
    def _compute_load(self):
        """ Compute the load on a local routing wire (number of on/partial/off) """
        
        # The first thing we are going to compute is how many local mux inputs are connected to a local routing wire
        # This is a function of local_mux size, N, K, I and Ofb
        num_local_routing_wires = self.I+self.N*self.num_ble_local_outputs
        self.mux_inputs_per_wire = self.local_mux_implemented_size*self.N*self.K/num_local_routing_wires
        
        # Now we compute how many "on" inputs are connected to each routing wire
        # This is a funtion of lut input usage, number of lut inputs and number of local routing wires
        num_local_muxes_used = self.lut_input_usage_assumption*self.N*self.K
        self.on_inputs_per_wire = int(num_local_muxes_used/num_local_routing_wires)
        # We want to model for the case where at least one "on" input is connected to the local wire, so make sure it's at least 1
        if self.on_inputs_per_wire < 1:
            self.on_inputs_per_wire = 1
        
        # Now we compute how many partially on muxes are connected to each wire
        # The number of partially on muxes is equal to (level2_size - 1)*num_local_muxes_used/num_local_routing_wire
        # We can figure out the number of muxes used by using the "on" assumption and the number of local routing wires.
        self.partial_inputs_per_wire = int((self.local_mux_l2_size - 1.0)*num_local_muxes_used/num_local_routing_wires)
        # Make it at least 1
        if self.partial_inputs_per_wire < 1:
            self.partial_inputs_per_wire = 1
        
        # Number of off inputs is simply the difference
        self.off_inputs_per_wire = self.mux_inputs_per_wire - self.on_inputs_per_wire - self.partial_inputs_per_wire
        

class _LogicCluster(_CompoundCircuit):
    
    def __init__(self, specs, carry_skip_periphery_count, local_mux_size_required, num_local_mux_per_tile):

        # Call the constructor of the base class
        _CompoundCircuit.__init__(self, "logic_cluster", specs.use_tgate, specs.use_finfet)

        # Number of BLEs in the logic cluster
        self.N = specs.N

        # Create BLE object
        self.ble = _BLE(specs, carry_skip_periphery_count)
        # Adding the BLE subcircuit to the subcircuits dictionary
        self.subcircuits[self.ble.name] = self.ble

        # Create local mux object
        self.local_mux = _LocalMUX(local_mux_size_required, num_local_mux_per_tile, specs.use_tgate)
        # Adding the local mux subcircuit to the subcircuits dictionary
        self.subcircuits[self.local_mux.name] = self.local_mux

        # Create local routing wire load object
        self.local_routing_wire_load = _LocalRoutingWireLoad(specs.I, specs.N, specs.K, 
            specs.num_ble_local_outputs, self.local_mux.level2_size, self.local_mux.implemented_size)
        # Adding the local routing wire load to the loads dictionary
        self.loads[self.local_routing_wire_load.name] = self.local_routing_wire_load

        #JUNIUS - add local crossbar for LUT skipping (update mode 10)
        if specs.updates == 10:
            # Create local mux object
            adder_sneak_paths = 20 # number of sneak paths used for adder-adder
            adder_direct_inputs = 4 # number of input pins connected directly to the FLUT CC Mux
            adder_direct_local_mux_size_required = int(adder_sneak_paths * specs.Fclocal)
            num_adder_direct_local_mux_per_tile = specs.N * adder_direct_inputs
            self.adder_direct_local_mux = _LocalMUX(adder_direct_local_mux_size_required, num_adder_direct_local_mux_per_tile, specs.use_tgate, name = 'adder_direct_local_mux')
            # Adding the local mux subcircuit to the subcircuits dictionary
            self.subcircuits[self.adder_direct_local_mux.name] = self.adder_direct_local_mux

            # Create local routing wire load object
            self.adder_direct_local_routing_wire_load = _LocalRoutingWireLoad(adder_sneak_paths, specs.N, adder_direct_inputs, 
                specs.num_ble_local_outputs, self.adder_direct_local_mux.level2_size, self.adder_direct_local_mux.implemented_size,
                name="adder_direct_local_routing_wire_load")
            # Adding the local routing wire load to the loads dictionary
            self.loads[self.adder_direct_local_routing_wire_load.name] = self.adder_direct_local_routing_wire_load
            
        if not specs.updates:
            # Create local BLE output load object
            self.local_ble_output_load = _LocalBLEOutputLoad()
            # Adding the local ble output load to the loads dictionary
            self.loads[self.local_ble_output_load.name] = self.local_ble_output_load

        # Boolean for using fracturable luts
        self.use_fluts = specs.use_fluts
        # Boolean for enabling carry chains in the logic blocks
        self.enable_carry_chain = specs.enable_carry_chain
        # Boolean indicationg that we are applying the new updates
        self.updates = specs.updates

        
    def generate(self, subcircuits_filename, specs):
        print "Generating logic cluster"
        init_tran_sizes = {}

        # Generate all the subcircuits and update the initial tran sizes dictionary
        for subcircuit in self.subcircuits.values():
            init_tran_sizes.update(subcircuit.generate(subcircuits_filename))

        # Generate all the loads
        for load in self.loads.values():
            load.generate(subcircuits_filename)

        return init_tran_sizes


    def generate_top(self):
        """ Generate the top SPICE file for all subcircuits """

        for subcircuit in self.subcircuits.values():
            subcircuit.generate_top()
        
        
    def update_area(self, area_dict, width_dict):
        """ Update areas for all subcircuits """ 

        for subcircuit in self.subcircuits.values():       
            subcircuit.update_area(area_dict, width_dict)
    
    def update_wires(self, width_dict, wire_lengths, wire_layers, ic_ratio, lut_ratio, ble_ic_dis, local_routing_wire_load_length):
        """ Update wires of things inside the logic cluster. """
        
        # Call wire update functions of member objects.
        self.ble.update_wires(width_dict, wire_lengths, wire_layers, lut_ratio)
        self.local_mux.update_wires(width_dict, wire_lengths, wire_layers, ic_ratio)
        self.local_routing_wire_load.update_wires(width_dict, wire_lengths, wire_layers, local_routing_wire_load_length)

        if not self.updates:
            self.local_ble_output_load.update_wires(width_dict, wire_lengths, wire_layers, ble_ic_dis)
        elif self.updates == 10:
            #JUNIUS - update adder direct local MUX in LUT skip (mode 10)
            self.adder_direct_local_mux.update_wires(width_dict, wire_lengths, wire_layers, ic_ratio)
            self.adder_direct_local_routing_wire_load.update_wires(width_dict, wire_lengths, wire_layers, local_routing_wire_load_length)
        
        
    def print_details(self, report_file):
        """ Print the details of all the subcircuits """

        self.local_mux.print_details(report_file)
        #JUNIUS - print details for LUT skip (mode 10)
        if self.updates == 10:
            self.adder_direct_local_mux.print_details(report_file)
        self.ble.print_details(report_file)
    
       
class _RoutingWireLoad:
    """ This is the routing wire load for an architecture with direct drive and only one segment length.
        Two-level muxes are assumed and we model for partially on paths. """
        
    def __init__(self, wire_length):
        # Name of this wire
        self.name = "routing_wire_load"
        # Length of wire (in tiles)
        self.wire_length = wire_length
        # We assume that half of the wires in a routing channel are used (limited by routability)
        self.channel_usage_assumption = 0.5
        # We assume that half of the cluster inputs are used
        self.cluster_input_usage_assumption = 0.5
        # Switch block load per wire
        self.sb_load_on = -1
        self.sb_load_partial = -1
        self.sb_load_off = -1
        # Connection block load per wire
        self.cb_load_on = -1
        self.cb_load_partial = -1
        self.cb_load_off = -1
        # Switch block per tile
        self.tile_sb_on = []
        self.tile_sb_partial = []
        self.tile_sb_off = []
        # Connection block per tile
        self.tile_cb_on = []
        self.tile_cb_partial = []
        self.tile_cb_off = []
        # List of wire names in the SPICE circuit
        self.wire_names = []
        
        
    def generate(self, subcircuit_filename, specs, sb_mux, cb_mux):
        """ Generate the SPICE circuit for general routing wire load
            Need specs object, switch block object and connection block object """
        print "Generating routing wire load"
        # Calculate wire load based on architecture parameters
        self._compute_load(specs, sb_mux, cb_mux, self.channel_usage_assumption, self.cluster_input_usage_assumption)
        # Generate SPICE deck
        self.wire_names = load_subcircuits.general_routing_load_generate(subcircuit_filename, self.wire_length, self.tile_sb_on, self.tile_sb_partial, self.tile_sb_off, self.tile_cb_on, self.tile_cb_partial, self.tile_cb_off)
    
    
    def update_wires(self, width_dict, wire_lengths, wire_layers, height, num_sb_stripes, num_cb_stripes):
        """ Calculate wire lengths and wire layers. """

        # This is the general routing wire that spans L tiles
        wire_lengths["wire_gen_routing"] = self.wire_length*width_dict["tile"]
        if height != 0.0:
            if height > ((width_dict["tile"]*width_dict["tile"])/height):
                wire_lengths["wire_gen_routing"] = self.wire_length*(height)
            else:
                wire_lengths["wire_gen_routing"] = self.wire_length*((width_dict["tile"]*width_dict["tile"])/height)

        # These are the pieces of wire that are required to connect routing wires to switch 
        # block inputs. We assume that on average, they span half a tile.
        wire_lengths["wire_sb_load_on"] = width_dict["tile"]/2
        wire_lengths["wire_sb_load_partial"] = width_dict["tile"]/2
        wire_lengths["wire_sb_load_off"] = width_dict["tile"]/2 
        if height != 0.0:
            if num_sb_stripes == 1:
                wire_lengths["wire_sb_load_on"] = wire_lengths["wire_gen_routing"]/self.wire_length
                wire_lengths["wire_sb_load_partial"] = wire_lengths["wire_gen_routing"]/self.wire_length
                wire_lengths["wire_sb_load_off"] = wire_lengths["wire_gen_routing"]/self.wire_length
            else:
                wire_lengths["wire_sb_load_on"] = wire_lengths["wire_gen_routing"]/(2*self.wire_length)
                wire_lengths["wire_sb_load_partial"] = wire_lengths["wire_gen_routing"]/(2*self.wire_length)
                wire_lengths["wire_sb_load_off"] = wire_lengths["wire_gen_routing"]/(2*self.wire_length)			
        # These are the pieces of wire that are required to connect routing wires to 
        # connection block multiplexer inputs. They span some fraction of a tile that is 
        # given my the input track-access span (track-access locality). 

        wire_lengths["wire_cb_load_on"] = width_dict["tile"]*INPUT_TRACK_ACCESS_SPAN
        wire_lengths["wire_cb_load_partial"] = width_dict["tile"]*INPUT_TRACK_ACCESS_SPAN
        wire_lengths["wire_cb_load_off"] = width_dict["tile"]*INPUT_TRACK_ACCESS_SPAN
        if height != 0 and num_cb_stripes == 1:
            wire_lengths["wire_cb_load_on"] = (wire_lengths["wire_gen_routing"]/self.wire_length) * 2
            wire_lengths["wire_cb_load_partial"] = (wire_lengths["wire_gen_routing"]/self.wire_length) * 2
            wire_lengths["wire_cb_load_off"] = (wire_lengths["wire_gen_routing"]/self.wire_length) * 2
        elif height != 0 :
            wire_lengths["wire_cb_load_on"] = (wire_lengths["wire_gen_routing"]/self.wire_length)
            wire_lengths["wire_cb_load_partial"] = (wire_lengths["wire_gen_routing"]/self.wire_length)
            wire_lengths["wire_cb_load_off"] = (wire_lengths["wire_gen_routing"]/self.wire_length)
			
       # Update wire layers
        wire_layers["wire_gen_routing"] = 1 
        wire_layers["wire_sb_load_on"] = 0 
        wire_layers["wire_sb_load_partial"] = 0 
        wire_layers["wire_sb_load_off"] = 0
        wire_layers["wire_cb_load_on"] = 0
        wire_layers["wire_cb_load_partial"] = 0 
        wire_layers["wire_cb_load_off"] = 0 
    
    
    def print_details(self, report_file):
        
        utils.print_and_write(report_file, "  ROUTING WIRE LOAD DETAILS:")
        utils.print_and_write(report_file, "  Number of SB inputs connected to routing wire = " + str(self.sb_load_on + self.sb_load_partial + self.sb_load_off))
        utils.print_and_write(report_file, "  Wire: SB (on = " + str(self.sb_load_on) + ", partial = " + str(self.sb_load_partial) + ", off = " + str(self.sb_load_off) + ")")
        utils.print_and_write(report_file, "  Number of CB inputs connected to routing wire = " + str(self.cb_load_on + self.cb_load_partial + self.cb_load_off))
        utils.print_and_write(report_file, "  Wire: CB (on = " + str(self.cb_load_on) + ", partial = " + str(self.cb_load_partial) + ", off = " + str(self.cb_load_off) + ")")

        for i in range(self.wire_length):
            utils.print_and_write(report_file, "  Tile " + str(i+1) + ": SB (on = " + str(self.tile_sb_on[i]) + ", partial = " + str(self.tile_sb_partial[i]) + 
            ", off = " + str(self.tile_sb_off[i]) + "); CB (on = " + str(self.tile_cb_on[i]) + ", partial = " + str(self.tile_cb_partial[i]) + ", off = " + str(self.tile_cb_off[i]) + ")")
        utils.print_and_write(report_file, "")
        
       
    def _compute_load(self, specs, sb_mux, cb_mux, channel_usage, cluster_input_usage):
        """ Computes the load on a routing wire """
        
        # Local variables
        W = specs.W
        L = specs.L
        I = specs.I
        Fs = specs.Fs
        sb_mux_size = sb_mux.implemented_size
        cb_mux_size = cb_mux.implemented_size
        sb_level1_size = sb_mux.level1_size
        sb_level2_size = sb_mux.level2_size
        cb_level1_size = cb_mux.level1_size
        cb_level2_size = cb_mux.level2_size
        
        # Calculate switch block load per tile
        # Each tile has Fs-1 switch blocks hanging off of it exept the last one which has 3 (because the wire is ending)
        sb_load_per_intermediate_tile = (Fs - 1)
        # Calculate number of on/partial/off
        # We assume that each routing wire is only driving one more routing wire (at the end)
        self.sb_load_on = 1
        # Each used routing multiplexer comes with (sb_level2_size - 1) partially on paths. 
        # If all wires were used, we'd have (sb_level2_size - 1) partially on paths per wire, TODO: Is this accurate? See ble output load
        # but since we are just using a fraction of the wires, each wire has (sb_level2_size - 1)*channel_usage partially on paths connected to it.
        self.sb_load_partial = int(round(float(sb_level2_size - 1.0)*channel_usage))
        # The number of off sb_mux is (total - partial)
        self.sb_load_off = sb_load_per_intermediate_tile*L - self.sb_load_partial
        
        # Calculate connection block load per tile
        # We assume that cluster inputs are divided evenly between horizontal and vertical routing channels
        # We can get the total number of CB inputs connected to the channel segment by multiplying cluster inputs by cb_mux_size, then divide by W to get cb_inputs/wire
        cb_load_per_tile = int(round((I/2*cb_mux_size)/W))
        # Now we got to find out how many are on, how many are partially on and how many are off
        # For each tile, we have half of the cluster inputs connecting to a routing channel and only a fraction of these inputs are actually used
        # It is logical to assume that used cluster inputs will be connected to used routing wires, so we have I/2*input_usage inputs per tile,
        # we have L tiles so, I/2*input_usage*L fully on cluster inputs connected to W*channel_usage routing wires
        # If we look at the whole wire, we are selecting I/2*input_usage*L signals from W*channel_usage wires
        cb_load_on_probability = float((I/2.0*cluster_input_usage*L))/(W*channel_usage)
        self.cb_load_on = int(round(cb_load_on_probability))
        # If < 1, we round up to one because at least one wire will have a fully on path connected to it and we model for that case.
        if self.cb_load_on == 0:
            self.cb_load_on = 1 
        # Each fully turned on cb_mux comes with (cb_level2_size - 1) partially on paths
        # The number of partially on paths per tile is I/2*input_usage * (cb_level2_size - 1) 
        # Number of partially on paths per wire is (I/2*input_usage * (cb_level2_size - 1) * L) / W
        cb_load_partial_probability = (I/2*cluster_input_usage * (cb_level2_size - 1) * L) / W
        self.cb_load_partial = int(round(cb_load_partial_probability))
        # If < 1, we round up to one because at least one wire will have a partially on path connected to it and we model for that case.
        if self.cb_load_partial == 0:
            self.cb_load_partial = 1 
        # Number of off paths is just number connected to routing wire - on - partial
        self.cb_load_off = cb_load_per_tile*L - self.cb_load_partial - self.cb_load_on
     
        # Now we want to figure out how to distribute this among the tiles. We have L tiles.
        tile_sb_on_budget = self.sb_load_on
        tile_sb_partial_budget = self.sb_load_partial
        tile_sb_off_budget = self.sb_load_off
        tile_sb_total_budget = tile_sb_on_budget + tile_sb_partial_budget + tile_sb_off_budget
        tile_sb_max = math.ceil(float(tile_sb_total_budget)/L)
        tile_sb_on = []
        tile_sb_partial = []
        tile_sb_off = []
        tile_sb_total = []

        # How this works: We have a certain amount of switch block mux connections to give to the wire,
        # we start at the furthest tile from the drive point and we allocate one mux input per tile iteratively until we run out of mux inputs.
        # The result of this is that on and partial mux inputs will be spread evenly along the wire with a bias towards putting 
        # them farthest away from the driver first (simulating a worst case).
        while tile_sb_total_budget != 0:
            # For each tile distribute load
            for i in range(L):
                # Add to lists
                if len(tile_sb_on) < (i+1):
                    tile_sb_on.append(0)
                if len(tile_sb_partial) < (i+1):
                    tile_sb_partial.append(0)
                if len(tile_sb_off) < (i+1):
                    tile_sb_off.append(0)
                if len(tile_sb_total) < (i+1):
                    tile_sb_total.append(0)
                # Distribute loads
                if tile_sb_on_budget != 0:
                    if tile_sb_total[i] != tile_sb_max:
                        tile_sb_on[i] = tile_sb_on[i] + 1
                        tile_sb_on_budget = tile_sb_on_budget - 1
                        tile_sb_total[i] = tile_sb_total[i] + 1
                        tile_sb_total_budget = tile_sb_total_budget - 1
                if tile_sb_partial_budget != 0:
                    if tile_sb_total[i] != tile_sb_max:
                        tile_sb_partial[i] = tile_sb_partial[i] + 1
                        tile_sb_partial_budget = tile_sb_partial_budget - 1
                        tile_sb_total[i] = tile_sb_total[i] + 1
                        tile_sb_total_budget = tile_sb_total_budget - 1
                if tile_sb_off_budget != 0:
                    if tile_sb_total[i] != tile_sb_max:
                        tile_sb_off[i] = tile_sb_off[i] + 1
                        tile_sb_off_budget = tile_sb_off_budget - 1
                        tile_sb_total[i] = tile_sb_total[i] + 1
                        tile_sb_total_budget = tile_sb_total_budget - 1
         
        # Assign these per-tile counts to the object
        self.tile_sb_on = tile_sb_on
        self.tile_sb_partial = tile_sb_partial
        self.tile_sb_off = tile_sb_off
         
        tile_cb_on_budget = self.cb_load_on
        tile_cb_partial_budget = self.cb_load_partial
        tile_cb_off_budget = self.cb_load_off
        tile_cb_total_budget = tile_cb_on_budget + tile_cb_partial_budget + tile_cb_off_budget
        tile_cb_max = math.ceil(float(tile_cb_total_budget)/L)
        tile_cb_on = []
        tile_cb_partial = []
        tile_cb_off = []
        tile_cb_total = []

        while tile_cb_total_budget != 0:
            # For each tile distribute load
            for i in range(L):
                # Add to lists
                if len(tile_cb_on) < (i+1):
                    tile_cb_on.append(0)
                if len(tile_cb_partial) < (i+1):
                    tile_cb_partial.append(0)
                if len(tile_cb_off) < (i+1):
                    tile_cb_off.append(0)
                if len(tile_cb_total) < (i+1):
                    tile_cb_total.append(0)
                # Distribute loads
                if tile_cb_on_budget != 0:
                    if tile_cb_total[i] != tile_cb_max:
                        tile_cb_on[i] = tile_cb_on[i] + 1
                        tile_cb_on_budget = tile_cb_on_budget - 1
                        tile_cb_total[i] = tile_cb_total[i] + 1
                        tile_cb_total_budget = tile_cb_total_budget - 1
                if tile_cb_partial_budget != 0:
                    if tile_cb_total[i] != tile_cb_max:
                        tile_cb_partial[i] = tile_cb_partial[i] + 1
                        tile_cb_partial_budget = tile_cb_partial_budget - 1
                        tile_cb_total[i] = tile_cb_total[i] + 1
                        tile_cb_total_budget = tile_cb_total_budget - 1
                if tile_cb_off_budget != 0:
                    if tile_cb_total[i] != tile_cb_max:
                        tile_cb_off[i] = tile_cb_off[i] + 1
                        tile_cb_off_budget = tile_cb_off_budget - 1
                        tile_cb_total[i] = tile_cb_total[i] + 1
                        tile_cb_total_budget = tile_cb_total_budget - 1
        
        # Assign these per-tile counts to the object
        self.tile_cb_on = tile_cb_on
        self.tile_cb_partial = tile_cb_partial
        self.tile_cb_off = tile_cb_off


class _pgateoutputcrossbar(_SizableCircuit):
    """ RAM outputcrossbar using pass transistors"""
    
    def __init__(self, maxwidth):
        # Subcircuit name
        self.name = "pgateoutputcrossbar"
        self.delay_weight = DELAY_WEIGHT_RAM
        self.maxwidth = maxwidth
        self.def_use_tgate = 0
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating BRAM output crossbar"
        

        # Call MUX generation function
        self.transistor_names, self.wire_names = memory_subcircuits.generate_pgateoutputcrossbar(subcircuit_filename, self.name, self.maxwidth, self.def_use_tgate)

        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 3
        self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 3
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 6
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 9
        if self.def_use_tgate == 0:
            self.initial_transistor_sizes["inv_" + self.name + "_3_nmos"] = 20
            self.initial_transistor_sizes["inv_" + self.name + "_3_pmos"] = 56
        else:
            self.initial_transistor_sizes["tgate_" + self.name + "_3_nmos"] = 1
            self.initial_transistor_sizes["tgate_" + self.name + "_3_pmos"] = 1
        self.initial_transistor_sizes["ptran_" + self.name + "_4_nmos"] = 1


        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level path for BRAM crossbar evaluation"
        self.top_spice_path = top_level.generate_pgateoutputcrossbar_top(self.name, self.maxwidth, self.def_use_tgate)


    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        current_count = self.maxwidth
        ptran_count = self.maxwidth

        while current_count >1:
            ptran_count += current_count/2
            current_count /=2

        ptran_count *=2
        ptran_count += self.maxwidth / 2

        area = (area_dict["inv_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"] + area_dict["inv_" + self.name + "_3"] * 2) * self.maxwidth + area_dict["ptran_" + self.name + "_4"]  * ptran_count
        #I'll use half of the area to obtain the width. This makes the process of defining wires easier for this crossbar
        width = math.sqrt(area)
        area *= 2
        area_with_sram = area + 2 * (self.maxwidth*2-1) * area_dict["sram"]
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram


    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        # Update wire lengths
        # We assume that the length of wire is the square root of output crossbar size
        # Another reasonable assumption is to assume it is equal to ram height or width
        # The latter, however, will result in very high delays for the output crossbar
        wire_lengths["wire_" + self.name] = width_dict[self.name + "_sram"]
        wire_layers["wire_" + self.name] = 0  


class _configurabledecoderiii(_SizableCircuit):
    """ Final stage of the configurable decoder"""
    
    def __init__(self, use_tgate, nand_size, fanin1, fanin2, tgatecount):
        # Subcircuit name
        self.name = "xconfigurabledecoderiii"
        self.required_size = nand_size
        self.use_tgate = use_tgate
        self.fanin1 = fanin1
        self.fanin2 = fanin2
        self.delay_weight = DELAY_WEIGHT_RAM
        self.tgatecount = tgatecount
    
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating  stage of the configurable decoder " + self.name
        

        # Call generation function
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoderiii(subcircuit_filename, self.name, self.required_size)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoderiii_lp(subcircuit_filename, self.name, self.required_size)

            print self.transistor_names
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        self.initial_transistor_sizes["inv_nand"+str(self.required_size)+"_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_nand"+str(self.required_size)+"_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 5
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 5
       # there is a wire in this cell, make sure to set its area to entire decoder

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation path for final stage of the configurable decoder"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_configurabledecoderiii_top(self.name, self.fanin1,self.fanin2, self.required_size, self.tgatecount)
        else:
            self.top_spice_path = top_level.generate_configurabledecoderiii_top_lp(self.name, self.fanin1,self.fanin2, self.required_size, self.tgatecount)

        
        

    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # predecoder area
        area = area_dict["inv_nand"+str(self.required_size)+"_" + self.name + "_1"]*self.required_size + area_dict["inv_" + self.name + "_2"]
        area_with_sram = area
          
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram


    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths
        wire_lengths["wire_" + self.name] = width_dict[self.name]
        wire_layers["wire_" + self.name] = 0


class _configurabledecoder3ii(_SizableCircuit):
    """ Second part of the configurable decoder"""
    
    def __init__(self, use_tgate, required_size, fan_out, fan_out_type, areafac):
        # Subcircuit name
        self.name = "xconfigurabledecoder3ii"
        self.required_size = required_size
        self.fan_out = fan_out
        self.fan_out_type = fan_out_type
        self.use_tgate = use_tgate
        self.areafac = areafac
    
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating second part of the configurable decoder" + self.name
        

        # Call generation function
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoder3ii(subcircuit_filename, self.name)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoder3ii_lp(subcircuit_filename, self.name)
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        self.initial_transistor_sizes["inv_nand3_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_nand3_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 1


        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evalation path for second part of the configurable decoder"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_configurabledecoder2ii_top(self.name, self.fan_out, 3)
        else:
            self.top_spice_path = top_level.generate_configurabledecoder2ii_top_lp(self.name, self.fan_out, 3)
   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # predecoder area
        area = (area_dict["inv_nand3_" + self.name + "_1"]*3 + area_dict["inv_" + self.name + "_2"])*self.areafac
        area_with_sram = area
          
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram




class _configurabledecoder2ii(_SizableCircuit):
    """ second part of the configurable decoder"""
    
    def __init__(self, use_tgate, required_size, fan_out, fan_out_type, areafac):
        # Subcircuit name
        self.name = "xconfigurabledecoder2ii"
        self.required_size = required_size
        self.fan_out = fan_out
        self.fan_out_type = fan_out_type
        self.use_tgate = use_tgate
        self.areafac = areafac
    
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating the second part of the configurable decoder" + self.name
        

        # Call generation function
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoder2ii(subcircuit_filename, self.name)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoder2ii_lp(subcircuit_filename, self.name)

            print self.transistor_names
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        self.initial_transistor_sizes["inv_nand2_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_nand2_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 1

       # there is a wire in this cell, make sure to set its area to entire decoder

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation path for second part of the configurable decoder"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_configurabledecoder2ii_top(self.name, self.fan_out, 2)
        else:
            self.top_spice_path = top_level.generate_configurabledecoder2ii_top_lp(self.name, self.fan_out, 2)
        
   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # predecoder area
        area = (area_dict["inv_nand2_" + self.name + "_1"]*2 + area_dict["inv_" + self.name + "_2"])*self.areafac
        area_with_sram = area
          
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram




class _configurabledecoderinvmux(_SizableCircuit):
    """ First stage of the configurable decoder"""
    # I assume that the configurable decoder has 2 stages. Hence it has between 4 bits and 9.
    # I don't think anyone would want to exceed that range!
    def __init__(self, use_tgate, numberofgates2,numberofgates3, ConfiDecodersize):
        self.name = "xconfigurabledecoderi"
        self.use_tgate = use_tgate
        self.ConfiDecodersize = ConfiDecodersize
        self.numberofgates2 = numberofgates2
        self.numberofgates3 = numberofgates3


    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating first stage of the configurable decoder" 
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoderi(subcircuit_filename, self.name)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_configurabledecoderi_lp(subcircuit_filename, self.name)
        self.initial_transistor_sizes["inv_xconfigurabledecoderi_1_nmos"] = 1 
        self.initial_transistor_sizes["inv_xconfigurabledecoderi_1_pmos"] = 1
        self.initial_transistor_sizes["tgate_xconfigurabledecoderi_2_nmos"] = 1 
        self.initial_transistor_sizes["tgate_xconfigurabledecoderi_2_pmos"] = 1
     
        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation path for the stage of the configurable decoder" 
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_configurabledecoderi_top(self.name,  self.numberofgates2, self.numberofgates3, self.ConfiDecodersize)
        else:
            self.top_spice_path = top_level.generate_configurabledecoderi_top_lp(self.name,  self.numberofgates2, self.numberofgates3, self.ConfiDecodersize)
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """     

        area = (area_dict["inv_xconfigurabledecoderi_1"] * self.ConfiDecodersize + 2* area_dict["tgate_xconfigurabledecoderi_2"])* self.ConfiDecodersize
        area_with_sram = area + self.ConfiDecodersize * area_dict["sram"] 

        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        wire_lengths["wire_" + self.name] = width_dict["configurabledecoder_wodriver"]
        wire_layers["wire_" + self.name] = 0

class _rowdecoder0(_SizableCircuit):
    """ Initial stage of the row decoder ( named stage 0) """
    def __init__(self, use_tgate, numberofgates3, valid_label_gates3, numberofgates2, valid_label_gates2, decodersize):
        self.name = "rowdecoderstage0"
        self.use_tgate = use_tgate
        self.decodersize = decodersize
        self.numberofgates2 = numberofgates2
        self.numberofgates3 = numberofgates3
        self.label3 = valid_label_gates3
        self.label2 = valid_label_gates2


    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating row decoder initial stage" 
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_rowdecoderstage0(subcircuit_filename, self.name)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_rowdecoderstage0_lp(subcircuit_filename, self.name)
        self.initial_transistor_sizes["inv_rowdecoderstage0_1_nmos"] = 9
        self.initial_transistor_sizes["inv_rowdecoderstage0_1_pmos"] = 9
     
        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating row decoder initial stage" 
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_rowdecoderstage0_top(self.name, self.numberofgates2, self.numberofgates3, self.decodersize, self.label2, self.label3)
        else:
            self.top_spice_path = top_level.generate_rowdecoderstage0_top_lp(self.name, self.numberofgates2, self.numberofgates3, self.decodersize, self.label2, self.label3)
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """     

        area = area_dict["inv_rowdecoderstage0_1"] * self.decodersize
        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # I assume that the wire in the row decoder is the sqrt of the area of the row decoder, including the wordline driver
        wire_lengths["wire_" + self.name] = math.sqrt(width_dict["decoder"]*width_dict["decoder"] + width_dict["wordline_total"]*width_dict["wordline_total"])
        wire_layers["wire_" + self.name] = 0



class _rowdecoder1(_SizableCircuit):
    """ Generating the first stage of the row decoder  """
    def __init__(self, use_tgate, fan_out, fan_out_type, nandtype, areafac):
        self.name = "rowdecoderstage1" + str(nandtype)
        self.use_tgate = use_tgate
        self.fanout = fan_out
        self.fanout_type = fan_out_type
        self.nandtype = nandtype
        self.areafac = areafac


    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating row decoder first stage" 

        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_rowdecoderstage1(subcircuit_filename, self.name, self.nandtype)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_rowdecoderstage1_lp(subcircuit_filename, self.name, self.nandtype)

        self.initial_transistor_sizes["inv_nand" + str(self.nandtype) + "_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_nand" + str(self.nandtype) + "_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 1

     
        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation path for row decoder first stage" 
        pass
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_rowdecoderstage1_top(self.name, self.fanout, self.nandtype)
        else:
            self.top_spice_path = top_level.generate_rowdecoderstage1_top_lp(self.name, self.fanout, self.nandtype)
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """ 

        area = (area_dict["inv_nand" + str(self.nandtype) + "_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"]) * self.areafac
        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram




class _wordlinedriver(_SizableCircuit):
    """ Wordline driver"""
    
    def __init__(self, use_tgate, rowsram, number_of_banks, areafac, is_rowdecoder_2stage, memory_technology):
        # Subcircuit name
        self.name = "wordline_driver"
        self.delay_weight = DELAY_WEIGHT_RAM
        self.rowsram = rowsram
        self.memory_technology = memory_technology
        self.number_of_banks = number_of_banks
        self.areafac = areafac
        self.is_rowdecoder_2stage = is_rowdecoder_2stage
        self.wl_repeater = 0
        if self.rowsram > 128:
            self.rowsram /= 2
            self.wl_repeater = 1
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating the wordline driver" + self.name

        # Call generation function
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_wordline_driver(subcircuit_filename, self.name, self.number_of_banks + 1, self.wl_repeater)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_wordline_driver_lp(subcircuit_filename, self.name, self.number_of_banks + 1, self.wl_repeater)

            print self.transistor_names
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)

        if self.number_of_banks == 1:
            self.initial_transistor_sizes["inv_nand2_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_nand2_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 1
        else:
            self.initial_transistor_sizes["inv_nand3_" + self.name + "_1_nmos"] = 1 
            self.initial_transistor_sizes["inv_nand3_" + self.name + "_1_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 1        

        self.initial_transistor_sizes["inv_" + self.name + "_3_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_3_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_4_nmos"] = 5
        self.initial_transistor_sizes["inv_" + self.name + "_4_pmos"] = 5
       # there is a wire in this cell, make sure to set its area to entire decoder

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation path for the wordline driver"
        pass 
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_wordline_driver_top(self.name, self.rowsram, self.number_of_banks + 1, self.is_rowdecoder_2stage, self.wl_repeater)
        else:
            self.top_spice_path = top_level.generate_wordline_driver_top_lp(self.name, self.rowsram, self.number_of_banks + 1, self.is_rowdecoder_2stage, self.wl_repeater)

    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # predecoder area
        #nand should be different than inv change later 
        area = area_dict["inv_" + self.name + "_3"] + area_dict["inv_" + self.name + "_4"]
        if self.wl_repeater == 1:
            area *=2
        if self.number_of_banks == 1:
            area+= area_dict["inv_nand2_" + self.name + "_1"]*2 + area_dict["inv_" + self.name + "_2"]
        else:
            area+= area_dict["inv_nand3_" + self.name + "_1"]*3 + area_dict["inv_" + self.name + "_2"]
        area = area * self.areafac

        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # Update wire lengths
        wire_lengths["wire_" + self.name] = wire_lengths["wire_memorycell_horizontal"]

        if self.memory_technology == "SRAM":
            wire_layers["wire_" + self.name] = 2
        else:
            wire_layers["wire_" + self.name] = 3



class _rowdecoderstage3(_SizableCircuit):
    """ third stage inside the row decoder"""
    
    def __init__(self, use_tgate, fanin1, fanin2, rowsram, gatesize, fanouttype, areafac):
        # Subcircuit name
        self.name = "rowdecoderstage3"
        self.fanin1 = fanin1
        self.fanin2 = fanin2
        self.fanout = fanouttype
        self.delay_weight = DELAY_WEIGHT_RAM
        self.rowsram = rowsram
        self.gatesize = gatesize
        self.areafac = areafac
    
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating last stage of the row decoder" + self.name

        # Call generation function
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_rowdecoderstage3(subcircuit_filename, self.name, self.fanout, self.gatesize - 1)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_rowdecoderstage3(subcircuit_filename, self.name, self.fanout, self.gatesize - 1)
        # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
        self.initial_transistor_sizes["inv_nand" + str(self.fanout) + "_" + self.name + "_1_nmos"] = 1
        self.initial_transistor_sizes["inv_nand" + str(self.fanout) + "_" + self.name + "_1_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 1
        if self.gatesize == 3:
            self.initial_transistor_sizes["inv_nand2_" + self.name + "_3_nmos"] = 1
            self.initial_transistor_sizes["inv_nand2_" + self.name + "_3_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_4_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_4_pmos"] = 1
        else:
            self.initial_transistor_sizes["inv_nand3_" + self.name + "_3_nmos"] = 1 
            self.initial_transistor_sizes["inv_nand3_" + self.name + "_3_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_4_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_4_pmos"] = 1        

        self.initial_transistor_sizes["inv_" + self.name + "_5_nmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_5_pmos"] = 1
        self.initial_transistor_sizes["inv_" + self.name + "_6_nmos"] = 5
        self.initial_transistor_sizes["inv_" + self.name + "_6_pmos"] = 5


        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation path for last stage of row decoder"
        pass 
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_rowdecoderstage3_top(self.name, self.fanin1, self.fanin2, self.rowsram, self.gatesize - 1, self.fanout)
        else:
            self.top_spice_path = top_level.generate_rowdecoderstage3_top_lp(self.name, self.fanin1, self.fanin2, self.rowsram, self.gatesize - 1, self.fanout)

    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        area = (area_dict["inv_nand" + str(self.fanout) + "_" + self.name + "_1"] + area_dict["inv_" + self.name + "_2"] + area_dict["inv_" + self.name + "_5"] + area_dict["inv_" + self.name + "_6"])
        if self.gatesize == 3:
            area+= area_dict["inv_nand2_" + self.name + "_3"]*2 + area_dict["inv_" + self.name + "_4"]
        else:
            area+= area_dict["inv_nand3_" + self.name + "_3"]*3 + area_dict["inv_" + self.name + "_4"]

        area = area * self.areafac
        area_with_sram = area
          
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram


class _powermtjread(_SizableCircuit):
    """ This class measures MTJ-based memory read power """
    def __init__(self, SRAM_per_column):

        self.name = "mtj_read_power"
        self.SRAM_per_column = SRAM_per_column
    def generate_top(self):
        print "Generating top level module to measure MTJ read power"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_mtj_read_power_top(self.name, self.SRAM_per_column)
        else:
            self.top_spice_path = top_level.generate_mtj_read_power_top_lp(self.name, self.SRAM_per_column)


class _powermtjwrite(_SizableCircuit):
    """ This class measures MTJ-based memory write power """
    def __init__(self, SRAM_per_column):

        self.name = "mtj_write_power"
        self.SRAM_per_column = SRAM_per_column
    def generate_top(self):
        print "Generating top level module to measure MTJ write power"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_mtj_write_power_top(self.name, self.SRAM_per_column)
        else:
            self.top_spice_path = top_level.generate_mtj_write_power_top_lp(self.name, self.SRAM_per_column)


class _powersramwritehh(_SizableCircuit):
    """ This class measures SRAM-based memory write power """
    def __init__(self, SRAM_per_column, column_multiplexity):

        self.name = "sram_writehh_power"
        self.SRAM_per_column = SRAM_per_column
        self.column_multiplexity = column_multiplexity

    def generate_top(self):
        print "Generating top level module to measure SRAM write power"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_sram_writehh_power_top(self.name, self.SRAM_per_column, self.column_multiplexity - 1)
        else:
            self.top_spice_path = top_level.generate_sram_writehh_power_top_lp(self.name, self.SRAM_per_column, self.column_multiplexity - 1)


class _powersramwritep(_SizableCircuit):
    """ This class measures SRAM-based memory write power """
    def __init__(self, SRAM_per_column, column_multiplexity):

        self.name = "sram_writep_power"
        self.SRAM_per_column = SRAM_per_column
        self.column_multiplexity = column_multiplexity

    def generate_top(self):
        print "Generating top level module to measure SRAM write power"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_sram_writep_power_top(self.name, self.SRAM_per_column, self.column_multiplexity - 1)
        else:
            self.top_spice_path = top_level.generate_sram_writep_power_top_lp(self.name, self.SRAM_per_column, self.column_multiplexity - 1)


class _powersramwritelh(_SizableCircuit):
    """ This class measures SRAM-based memory write power """
    def __init__(self, SRAM_per_column, column_multiplexity):

        self.name = "sram_writelh_power"
        self.SRAM_per_column = SRAM_per_column
        self.column_multiplexity = column_multiplexity

    def generate_top(self):
        print "Generating top level module to measure SRAM write power"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_sram_writelh_power_top(self.name, self.SRAM_per_column, self.column_multiplexity - 1)
        else:
            self.top_spice_path = top_level.generate_sram_writelh_power_top_lp(self.name, self.SRAM_per_column, self.column_multiplexity - 1)


class _powersramread(_SizableCircuit):
    """ This class measures SRAM-based memory read power """
    def __init__(self, SRAM_per_column, column_multiplexity):

        self.name = "sram_read_power"
        self.SRAM_per_column = SRAM_per_column
        self.column_multiplexity = column_multiplexity

    def generate_top(self):
        print "Generating top level module to measure SRAM read power"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_sram_read_power_top(self.name, self.SRAM_per_column, self.column_multiplexity - 1)
        else:
            self.top_spice_path = top_level.generate_sram_read_power_top_lp(self.name, self.SRAM_per_column, self.column_multiplexity - 1)

class _columndecoder(_SizableCircuit):
    """ Column decoder"""

    def __init__(self, use_tgate, numberoftgates, col_decoder_bitssize):
        self.name = "columndecoder"
        self.use_tgate = use_tgate
        self.col_decoder_bitssize = col_decoder_bitssize
        self.numberoftgates = numberoftgates


    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating column decoder " 
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_columndecoder(subcircuit_filename, self.name, self.col_decoder_bitssize)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_columndecoder_lp(subcircuit_filename, self.name, self.col_decoder_bitssize)
        self.initial_transistor_sizes["inv_columndecoder_1_nmos"] = 1 
        self.initial_transistor_sizes["inv_columndecoder_1_pmos"] = 1
        self.initial_transistor_sizes["inv_columndecoder_2_nmos"] = 1 
        self.initial_transistor_sizes["inv_columndecoder_2_pmos"] = 1
        self.initial_transistor_sizes["inv_columndecoder_3_nmos"] = 2 
        self.initial_transistor_sizes["inv_columndecoder_3_pmos"] = 2

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation path for column decoder"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_columndecoder_top(self.name,  self.numberoftgates, self.col_decoder_bitssize)
        else:
            self.top_spice_path = top_level.generate_columndecoder_top_lp(self.name,  self.numberoftgates, self.col_decoder_bitssize)

    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """     

        area = area_dict["inv_columndecoder_1"] * self.col_decoder_bitssize +area_dict["inv_columndecoder_2"]*self.col_decoder_bitssize * 2**self.col_decoder_bitssize + area_dict["inv_columndecoder_3"] * 2**self.col_decoder_bitssize
        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        pass


class _writedriver(_SizableCircuit):
    """ SRAM-based BRAM Write driver"""

    def __init__(self, use_tgate, numberofsramsincol):
        self.name = "writedriver"
        self.use_tgate = use_tgate
        self.numberofsramsincol = numberofsramsincol


    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating write driver" 
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_writedriver(subcircuit_filename, self.name)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_writedriver_lp(subcircuit_filename, self.name)

        # Sizing according to Kosuke
        self.initial_transistor_sizes["inv_writedriver_1_nmos"] = 1.222 
        self.initial_transistor_sizes["inv_writedriver_1_pmos"] = 2.444
        self.initial_transistor_sizes["inv_writedriver_2_nmos"] = 1.222
        self.initial_transistor_sizes["inv_writedriver_2_pmos"] = 2.444
        self.initial_transistor_sizes["tgate_writedriver_3_nmos"] = 5.555
        self.initial_transistor_sizes["tgate_writedriver_3_pmos"] = 3.333
        

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level evaluation for write driver"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_writedriver_top(self.name, self.numberofsramsincol)
        else:
            self.top_spice_path = top_level.generate_writedriver_top_lp(self.name, self.numberofsramsincol)

    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """     

        area = area_dict["inv_writedriver_1"] + area_dict["inv_writedriver_2"] + area_dict["tgate_writedriver_3"]* 4
        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram


class _samp(_SizableCircuit):
    """ sense amplifier circuit"""

    def __init__(self, use_tgate, numberofsramsincol, mode, difference):
        self.name = "samp1"
        self.use_tgate = use_tgate
        self.numberofsramsincol = numberofsramsincol
        self.mode = mode
        self.difference = difference

    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating sense amplifier circuit" 
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_samp(subcircuit_filename, self.name)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_samp_lp(subcircuit_filename, self.name)
        self.initial_transistor_sizes["inv_samp_output_1_nmos"] = 1 
        self.initial_transistor_sizes["inv_samp_output_1_pmos"] = 1
        #its not actually a PTRAN. Doing this only for area calculation:
        self.initial_transistor_sizes["ptran_samp_output_1_pmos"] = 5.555
        self.initial_transistor_sizes["ptran_samp_output_2_nmos"] = 2.222
        self.initial_transistor_sizes["ptran_samp_output_3_nmos"] = 20.0
        self.initial_transistor_sizes["ptran_samp_output_4_nmos"] = 5.555
        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating sense amplifier circuit"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_samp_top_part1(self.name, self.numberofsramsincol, self.difference)
        else:
            self.top_spice_path = top_level.generate_samp_top_part1_lp(self.name, self.numberofsramsincol, self.difference)
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """     

        area = area_dict["inv_samp_output_1"] + area_dict["ptran_samp_output_1"]*2 +  area_dict["ptran_samp_output_2"]*2 + area_dict["ptran_samp_output_3"]*2 +  area_dict["ptran_samp_output_1"]
        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram



class _samp_part2(_SizableCircuit):
    """ sense amplifier circuit (second evaluation stage)"""

    def __init__(self, use_tgate, numberofsramsincol, difference):
        self.name = "samp1part2"
        self.use_tgate = use_tgate
        self.numberofsramsincol = numberofsramsincol
        self.difference = difference


    def generate_top(self):
        print "Generating top-level evaluation path for the second stage of sense amplifier"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_samp_top_part2(self.name, self.numberofsramsincol, self.difference)
        else:
            self.top_spice_path = top_level.generate_samp_top_part2_lp(self.name, self.numberofsramsincol, self.difference)



class _prechargeandeq(_SizableCircuit):
    """ precharge and equalization circuit"""

    def __init__(self, use_tgate, numberofsrams):
        self.name = "precharge"
        self.use_tgate = use_tgate
        self.numberofsrams = numberofsrams


    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating precharge and equalization circuit" 
        if use_lp_transistor == 0:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_precharge(subcircuit_filename, self.name)
        else:
            self.transistor_names, self.wire_names = memory_subcircuits.generate_precharge_lp(subcircuit_filename, self.name)

        self.initial_transistor_sizes["ptran_precharge_side_nmos"] = 15
        self.initial_transistor_sizes["ptran_equalization_nmos"] = 1

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating precharge and equalization circuit"
        if use_lp_transistor == 0:
            self.top_spice_path = top_level.generate_precharge_top(self.name, self.numberofsrams)
        else:
            self.top_spice_path = top_level.generate_precharge_top_lp(self.name, self.numberofsrams)
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """     

        area = area_dict["ptran_precharge_side"]*2 + area_dict["ptran_equalization"]
        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        pass


class _levelshifter(_SizableCircuit):
    "Level Shifter"

    def __init__(self):
        self.name = "level_shifter"

    def generate(self, subcircuit_filename):
        print "Generating the level shifter" 

        self.transistor_names = []
        self.transistor_names.append(memory_subcircuits.generate_level_shifter(subcircuit_filename, self.name))

        self.initial_transistor_sizes["inv_level_shifter_1_nmos"] = 1
        self.initial_transistor_sizes["inv_level_shifter_1_pmos"] = 1.6667
        self.initial_transistor_sizes["ptran_level_shifter_2_nmos"] = 1
        self.initial_transistor_sizes["ptran_level_shifter_3_pmos"] = 1

        return self.initial_transistor_sizes

    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """

        area_dict[self.name] = area_dict["inv_level_shifter_1"] * 3 + area_dict["ptran_level_shifter_2"] * 2 + area_dict["ptran_level_shifter_3"] * 2



class _mtjsamp(_SizableCircuit):
    "MTJ sense amplifier operation"

    def __init__(self, colsize):
        self.name = "mtj_samp_m"
        self.colsize = colsize

    def generate_top(self):
        print "generating MTJ sense amp operation"

        self.top_spice_path = top_level.generate_mtj_sa_top(self.name, self.colsize)


class _mtjblcharging(_SizableCircuit):
    "Bitline charging in MTJ"

    def __init__(self, colsize):
        self.name = "mtj_charge"
        self.colsize = colsize

    def generate_top(self):
        print "generating top level circuit for MTJ charging process"

        self.top_spice_path = top_level.generate_mtj_charge(self.name, self.colsize)

class _mtjbldischarging(_SizableCircuit):
    "Bitline discharging in MTJ"

    def __init__(self, colsize):
        self.name = "mtj_discharge"
        self.colsize = colsize


    def generate(self, subcircuit_filename, min_tran_width):
        "Bitline discharging in MTJ"
        self.transistor_names = []
        self.transistor_names.append("ptran_mtj_subcircuits_mtjcs_0_nmos")

        self.initial_transistor_sizes["ptran_mtj_subcircuits_mtjcs_0_nmos"] = 5

        return self.initial_transistor_sizes


    def generate_top(self):
        print "generating top level circuit for MTJ discharging process"

        self.top_spice_path = top_level.generate_mtj_discharge(self.name, self.colsize)


class _mtjbasiccircuits(_SizableCircuit):
    """ MTJ subcircuits"""
    def __init__(self):
        self.name = "mtj_subcircuits"

    def generate(self, subcircuit_filename):
        print "Generating MTJ subcircuits" 

        self.transistor_names = []
        self.transistor_names.append(memory_subcircuits.generate_mtj_sa_lp(subcircuit_filename, self.name))
        self.transistor_names.append(memory_subcircuits.generate_mtj_writedriver_lp(subcircuit_filename, self.name))
        self.transistor_names.append(memory_subcircuits.generate_mtj_cs_lp(subcircuit_filename, self.name))


        self.initial_transistor_sizes["ptran_mtj_subcircuits_mtjsa_1_pmos"] = 6.6667
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjsa_2_nmos"] = 17.7778
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjsa_2_pmos"] = 2.2222
        self.initial_transistor_sizes["ptran_mtj_subcircuits_mtjsa_3_nmos"] = 5.5556
        self.initial_transistor_sizes["ptran_mtj_subcircuits_mtjsa_4_nmos"] = 3.6667
        self.initial_transistor_sizes["ptran_mtj_subcircuits_mtjsa_5_nmos"] = 4.4444
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjsa_6_nmos"] = 1
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjsa_6_pmos"] = 1

        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjwd_1_nmos"] = 13.3333
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjwd_1_pmos"] = 13.3333
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjwd_2_nmos"] = 13.3333
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjwd_2_pmos"] = 13.3333
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjwd_3_nmos"] = 1
        self.initial_transistor_sizes["inv_mtj_subcircuits_mtjwd_3_pmos"] = 2

        self.initial_transistor_sizes["tgate_mtj_subcircuits_mtjcs_1_pmos"] = 13.3333
        self.initial_transistor_sizes["tgate_mtj_subcircuits_mtjcs_1_nmos"] = 13.3333
        

        return self.initial_transistor_sizes

    
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """

        area_dict[self.name + "_sa"] = 2* (area_dict["ptran_mtj_subcircuits_mtjsa_1"] + area_dict["inv_mtj_subcircuits_mtjsa_2"] + area_dict["ptran_mtj_subcircuits_mtjsa_3"] + area_dict["ptran_mtj_subcircuits_mtjsa_4"] + area_dict["ptran_mtj_subcircuits_mtjsa_3"] * 2)
        area_dict[self.name + "_sa"] += area_dict["inv_mtj_subcircuits_mtjsa_6"] * 2
        width_dict[self.name + "_sa"] = math.sqrt(area_dict[self.name + "_sa"])

        area_dict[self.name + "_writedriver"] = area_dict["inv_mtj_subcircuits_mtjwd_1"] + area_dict["inv_mtj_subcircuits_mtjwd_2"] + area_dict["inv_mtj_subcircuits_mtjwd_3"]
        width_dict[self.name + "_writedriver"] = math.sqrt(area_dict[self.name + "_writedriver"])

        area_dict[self.name + "_cs"] = area_dict["tgate_mtj_subcircuits_mtjcs_1"]  + area_dict["ptran_mtj_subcircuits_mtjcs_0"]
        width_dict[self.name + "_cs"] = math.sqrt(area_dict[self.name + "_cs"])

        # this is a dummy area for the timing path that I size in transistor sizing stage.
        # the purpose of adding this is to avoid changing the code in transistor sizing stage as this is the only exception.
        area_dict["mtj_discharge"] = 0


class _memorycell(_SizableCircuit):
    """ Memory cell"""

    def __init__(self, use_tgate, RAMwidth, RAMheight, sram_area, number_of_banks, memory_technology):
        self.name = "memorycell"
        self.use_tgate = use_tgate
        self.RAMwidth = RAMwidth
        self.RAMheight = RAMheight
        if memory_technology == "SRAM":
            self.wirevertical =  204 * RAMheight
            self.wirehorizontal = 830 * RAMwidth
        else:
            self.wirevertical =  204 * RAMheight
            self.wirehorizontal = 205 * RAMwidth            
        self.number_of_banks = number_of_banks
        self.memory_technology = memory_technology

    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating BRAM memorycell" 

        if self.memory_technology == "SRAM":
            if use_lp_transistor == 0:
                self.transistor_names, self.wire_names = memory_subcircuits.generate_memorycell(subcircuit_filename, self.name)
            else:
                self.transistor_names, self.wire_names = memory_subcircuits.generate_memorycell_lp(subcircuit_filename, self.name)
        else:
            memory_subcircuits.generate_mtj_memorycell_high_lp(subcircuit_filename, self.name)
            memory_subcircuits.generate_mtj_memorycell_low_lp(subcircuit_filename, self.name)
            memory_subcircuits.generate_mtj_memorycell_reference_lp(subcircuit_filename, self.name)
            memory_subcircuits.generate_mtj_memorycellh_reference_lp(subcircuit_filename, self.name)
            memory_subcircuits.generate_mtj_memorycell_reference_lp_target(subcircuit_filename, self.name)


    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """
        if self.memory_technology == "SRAM":     
            area = area_dict["ramsram"] * self.RAMheight * self.RAMwidth * self.number_of_banks
        else:
            area = area_dict["rammtj"] * self.RAMheight * self.RAMwidth * self.number_of_banks
        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram

    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # In this function, we determine the size of two wires and use them very frequently
        # The horizontal and vertical wires are determined by width and length of memory cells and their physical arrangement
        wire_lengths["wire_" + self.name + "_horizontal"] = self.wirehorizontal
        wire_layers["wire_" + self.name + "_horizontal"] = 2
        wire_lengths["wire_" + self.name + "_vertical"] = self.wirevertical
        wire_layers["wire_" + self.name + "_vertical"] = 2



class _RAMLocalMUX(_SizableCircuit):
    """ RAM Local MUX Class: Pass-transistor 2-level mux with no driver """
    
    def __init__(self, required_size, num_per_tile, use_tgate):
        # Subcircuit name
        #sadegh
        self.name = "ram_local_mux"
        # How big should this mux be (dictated by architecture specs)
        self.required_size = required_size 
        # How big did we make the mux (it is possible that we had to make the mux bigger for level sizes to work out, this is how big the mux turned out)
        self.implemented_size = -1
        # This is simply the implemented_size-required_size
        self.num_unused_inputs = -1
        # Number of switch block muxes in one FPGA tile
        self.num_per_tile = num_per_tile
        # Number of SRAM cells per mux
        self.sram_per_mux = -1
        # Size of the first level of muxing
        self.level1_size = -1
        # Size of the second level of muxing
        self.level2_size = -1
        # Delay weight in a representative critical path
        self.delay_weight = DELAY_WEIGHT_RAM
        # use pass transistor or transmission gates
        self.use_tgate = use_tgate
    
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating RAM local mux"
        
        # Calculate level sizes and number of SRAMs per mux
        self.level2_size = int(math.sqrt(self.required_size))
        self.level1_size = int(math.ceil(float(self.required_size)/self.level2_size))
        self.implemented_size = self.level1_size*self.level2_size
        self.num_unused_inputs = self.implemented_size - self.required_size
        self.sram_per_mux = self.level1_size + self.level2_size
        
        if not self.use_tgate :
            # Call generation function
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2lvl_mux_no_driver(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_" + self.name + "_L2_nmos"] = 2
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 2

        else :
            # Call MUX generation function
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2lvl_mux_no_driver(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_pmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_nmos"] = 2
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 2


        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level RAM local mux"

        self.top_spice_path = top_level.generate_RAM_local_mux_top(self.name)

   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # MUX area
        if not self.use_tgate :
            area = ((self.level1_size*self.level2_size)*area_dict["ptran_" + self.name + "_L1"] +
                    self.level2_size*area_dict["ptran_" + self.name + "_L2"] +
                    area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"])
        else :
            area = ((self.level1_size*self.level2_size)*area_dict["tgate_" + self.name + "_L1"] +
                    self.level2_size*area_dict["tgate_" + self.name + "_L2"] +
                    # area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"])
          
        # MUX area including SRAM
        area_with_sram = (area + (self.level1_size + self.level2_size)*area_dict["sram"])
          
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram



    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths

        wire_lengths["wire_" + self.name + "_L1"] = width_dict[self.name]
        wire_lengths["wire_" + self.name + "_L2"] = width_dict[self.name]
        
        # Update wire layers
        wire_layers["wire_" + self.name + "_L1"] = 0
        wire_layers["wire_" + self.name + "_L2"] = 0  



   
    def print_details(self, report_file):
        """ Print RAM local mux details """
        
        utils.print_and_write(report_file, "  RAM LOCAL MUX DETAILS:")
        utils.print_and_write(report_file, "  Style: two-level MUX")
        utils.print_and_write(report_file, "  Required MUX size: " + str(self.required_size) + ":1")
        utils.print_and_write(report_file, "  Implemented MUX size: " + str(self.implemented_size) + ":1")
        utils.print_and_write(report_file, "  Level 1 size = " + str(self.level1_size))
        utils.print_and_write(report_file, "  Level 2 size = " + str(self.level2_size))
        utils.print_and_write(report_file, "  Number of unused inputs = " + str(self.num_unused_inputs))
        utils.print_and_write(report_file, "  Number of MUXes per tile: " + str(self.num_per_tile))
        utils.print_and_write(report_file, "  Number of SRAM cells per MUX: " + str(self.sram_per_mux))
        utils.print_and_write(report_file, "")


class _RAMLocalRoutingWireLoad:
    """ Local routing wire load """
    
    def __init__(self, row_decoder_bits, col_decoder_bits, conf_decoder_bits):
        # Name of this wire
        self.name = "local_routing_wire_load"
        # This is calculated for the widest mode (worst case scenario. Other modes have less usage)
        self.RAM_input_usage_assumption = float((2 + 2*(row_decoder_bits + col_decoder_bits) + 2** (conf_decoder_bits))/(2 + 2*(row_decoder_bits + col_decoder_bits+ conf_decoder_bits) + 2** (conf_decoder_bits)))
        # Total number of local mux inputs per wire
        self.mux_inputs_per_wire = -1
        # Number of on inputs connected to each wire 
        self.on_inputs_per_wire = -1
        # Number of partially on inputs connected to each wire
        self.partial_inputs_per_wire = -1
        #Number of off inputs connected to each wire
        self.off_inputs_per_wire = -1
        # List of wire names in the SPICE circuit
        self.wire_names = []
        self.row_decoder_bits = row_decoder_bits
        self.col_decoder_bits = col_decoder_bits
        self.conf_decoder_bits = conf_decoder_bits


    def generate(self, subcircuit_filename, specs, RAM_local_mux):
        print "Generating local routing wire load"
        # Compute load (number of on/partial/off per wire)
        self._compute_load(specs, RAM_local_mux)
        # Generate SPICE deck
        self.wire_names = load_subcircuits.RAM_local_routing_load_generate(subcircuit_filename, self.on_inputs_per_wire, self.partial_inputs_per_wire, self.off_inputs_per_wire)
    
    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # Update wire lengths
        # wire_lengths["wire_ram_local_routing"] = width_dict["ram_local_mux_total"]
        wire_lengths["wire_ram_local_routing"] = width_dict["ram_local_mux_total"]
        # Update wire layers
        wire_layers["wire_ram_local_routing"] = 0
    
        
        
        
    def _compute_load(self, specs, RAM_local_mux):
        """ Compute the load on a local routing wire (number of on/partial/off) """
        
        # The first thing we are going to compute is how many local mux inputs are connected to a local routing wire
        # FOR a ram block its the number of inputs
        num_local_routing_wires = (2 + 2*(self.row_decoder_bits + self.col_decoder_bits+ self.conf_decoder_bits) + 2** (self.conf_decoder_bits))
        self.mux_inputs_per_wire = RAM_local_mux.implemented_size
        
        # Now we compute how many "on" inputs are connected to each routing wire
        # This is a funtion of lut input usage, number of lut inputs and number of local routing wires
        num_local_muxes_used = self.RAM_input_usage_assumption*(2 + 2*(self.row_decoder_bits + self.col_decoder_bits+ self.conf_decoder_bits) + 2** (self.conf_decoder_bits))
        self.on_inputs_per_wire = int(num_local_muxes_used/num_local_routing_wires)
        # We want to model for the case where at least one "on" input is connected to the local wire, so make sure it's at least 1
        if self.on_inputs_per_wire < 1:
            self.on_inputs_per_wire = 1
        
        # Now we compute how many partially on muxes are connected to each wire
        # The number of partially on muxes is equal to (level2_size - 1)*num_local_muxes_used/num_local_routing_wire
        # We can figure out the number of muxes used by using the "on" assumption and the number of local routing wires.
        self.partial_inputs_per_wire = int((RAM_local_mux.level2_size - 1.0)*num_local_muxes_used/num_local_routing_wires)
        # Make it at least 1
        if self.partial_inputs_per_wire < 1:
            self.partial_inputs_per_wire = 1
        
        # Number of off inputs is simply the difference
        self.off_inputs_per_wire = self.mux_inputs_per_wire - self.on_inputs_per_wire - self.partial_inputs_per_wire

class _RAM(_CompoundCircuit):
    
    def __init__(self, row_decoder_bits, col_decoder_bits, conf_decoder_bits, RAM_local_mux_size_required, RAM_num_local_mux_per_tile, use_tgate ,sram_area, number_of_banks, memory_technology, cspecs, process_data_filename, read_to_write_ratio):
        # Name of RAM block
        self.name = "ram"
        # use tgates or pass transistors
        self.use_tgate = use_tgate
        # RAM info such as docoder size, crossbar population, number of banks and output crossbar fanout
        self.row_decoder_bits = row_decoder_bits
        self.col_decoder_bits = col_decoder_bits
        self.conf_decoder_bits = conf_decoder_bits
        self.number_of_banks = number_of_banks
        self.cspecs = cspecs
        self.target_bl = 0.04
        self.process_data_filename = process_data_filename
        self.memory_technology = memory_technology

        # timing components
        self.T1 = 0.0
        self.T2 = 0.0
        self.T3 = 0.0
        self.frequency = 0.0

        # Energy components
        self.read_to_write_ratio = read_to_write_ratio
        self.core_energy = 0.0
        self.peripheral_energy_read = 0.0
        self.peripheral_energy_write = 0.0

        # This is the estimated row decoder delay
        self.estimated_rowdecoder_delay = 0.0

        if number_of_banks == 2:
            self.row_decoder_bits = self.row_decoder_bits - 1

        # delay weight of the RAM module in total delay measurement
        self.delay_weight = DELAY_WEIGHT_RAM

        # Create local mux object
        self.RAM_local_mux = _RAMLocalMUX(RAM_local_mux_size_required, RAM_num_local_mux_per_tile, use_tgate)
        # Create the local routing wire load module
        self.RAM_local_routing_wire_load = _RAMLocalRoutingWireLoad(self.row_decoder_bits, col_decoder_bits, conf_decoder_bits)
        # Create memory cells
        self.memorycells = _memorycell(self.use_tgate, 2**(conf_decoder_bits + col_decoder_bits), 2**(self.row_decoder_bits), sram_area, self.number_of_banks, self.memory_technology)
        # calculat the number of input ports for the ram module
        self.ram_inputs = (3 + 2*(self.row_decoder_bits + col_decoder_bits + number_of_banks ) + 2** (self.conf_decoder_bits))
        
        #initialize decoder object sizes
        # There are two predecoders, each of which can have up two three internal predecoders as well:
        self.predecoder1 = 1
        self.predecoder2 = 2
        self.predecoder3 = 0
        # Create decoder object
        # determine the number of predecoders, 2 predecoders are needed
        # inside each predecoder, 2 or 3 nandpaths are required as first stage.
        # the second stage, is a 2 input or 3 input nand gate, there are lots of 2nd stage nand gates as load for the first stage
        # a width of less than 8  will use smaller decoder (2 levels) while a larger decoder uses 3 levels



        # If there are two banks, the rowdecoder size is reduced in half (number of bits is reduced by 1)

        # Bounds on row decoder size:
        assert self.row_decoder_bits >= 5
        assert self.row_decoder_bits <= 9

        # determine decoder object sizes
        # The reason why I'm allocating predecoder size like this is that it gives the user a better flexibility to determine how to make their decoders
        # For example, if the user doesn't want to have 2 predecoders when decoding 6 bits, they can simply change the number below.
        # Feel free to change the following to determine how your decoder is generated.
        if self.row_decoder_bits == 5:
            self.predecoder1 = 3
            self.predecoder2 = 2
        if self.row_decoder_bits == 6:
            self.predecoder1 = 3
            self.predecoder2 = 3
        if self.row_decoder_bits == 7:
            self.predecoder1 = 3
            self.predecoder2 = 2
            self.predecoder3 = 2
        if self.row_decoder_bits == 8:
            self.predecoder1 = 3
            self.predecoder2 = 3
            self.predecoder3 = 2
        if self.row_decoder_bits == 9:
            self.predecoder1 = 3
            self.predecoder2 = 3
            self.predecoder3 = 3



        # some variables to determine size of decoder stages and their numbers
        self.valid_row_dec_size2 = 0
        self.valid_row_dec_size3 = 0
        self.count_row_dec_size2 = 0
        self.count_row_dec_size3 = 0
        self.fanout_row_dec_size2 = 0
        self.fanout_row_dec_size3 = 0
        self.fanouttypeforrowdec = 2

        

        #small row decoder
        #figure out how many stage 0 nand gates u have and of what type
        self.count_small_row_dec_nand2 = 0
        self.count_small_row_dec_nand3 = 0
        if self.predecoder1 == 3:
            self.count_small_row_dec_nand3 = self.count_small_row_dec_nand3 + 3
        else:
            self.count_small_row_dec_nand2 = self.count_small_row_dec_nand2 + 2

        if self.predecoder2 == 3:
            self.count_small_row_dec_nand3 = self.count_small_row_dec_nand3 + 3
        else:
            self.count_small_row_dec_nand2 = self.count_small_row_dec_nand2 + 2               

        if self.predecoder3 == 3:
            self.count_small_row_dec_nand3 = self.count_small_row_dec_nand3 + 3
        elif self.predecoder3 == 2:
            self.count_small_row_dec_nand2 = self.count_small_row_dec_nand2 + 2 

        self.rowdecoder_stage0 = _rowdecoder0(use_tgate, self.count_small_row_dec_nand3,0 , self.count_small_row_dec_nand2, 0 , self.row_decoder_bits)
        #generate stage 0 , just an inverter that connects to half of the gates
        #call a function to generate it!!
        # Set up the wordline driver
        self.wordlinedriver = _wordlinedriver(use_tgate, 2**(self.col_decoder_bits + self.conf_decoder_bits), self.number_of_banks, 2**self.row_decoder_bits, 1, self.memory_technology)

        # create stage 1 similiar to configurable decoder

        if self.predecoder3 !=0:
            self.fanouttypeforrowdec = 3

        self.area2 = 0
        self.area3 = 0

        if self.predecoder2 == 2:
            self.area2 += 4
        else:
            self.area3 +=8

        if self.predecoder1 == 2:
            self.area2 += 4
        else:
            self.area3 +=8             

        if self.predecoder3 == 2:
            self.area2 += 4
        elif self.predecoder3 ==3:
            self.area3 +=8

        #check if nand2 exists, call a function
        if self.predecoder1 == 2 or self.predecoder2 == 2 or self.predecoder3 == 2:
            self.rowdecoder_stage1_size2 = _rowdecoder1(use_tgate, 2 ** (self.row_decoder_bits - 2), self.fanouttypeforrowdec, 2, self.area2)
            self.valid_row_dec_size2 = 1

        #check if nand3 exists, call a function
        if self.predecoder1 == 3 or self.predecoder2 == 3 or self.predecoder3 == 3:
            self.rowdecoder_stage1_size3 = _rowdecoder1(use_tgate, 2 ** (self.row_decoder_bits - 3), self.fanouttypeforrowdec, 3, self.area3)
            self.valid_row_dec_size3 = 1

        # there is no intermediate stage, connect what you generated directly to stage 3
        self.gatesize_stage3 = 2
        if self.row_decoder_bits > 6:
            self.gatesize_stage3 = 3

        self.rowdecoder_stage3 = _rowdecoderstage3(use_tgate, self.area2, self.area3, 2**(self.col_decoder_bits + self.conf_decoder_bits), self.gatesize_stage3, self.fanouttypeforrowdec, 2** self.row_decoder_bits)



        # measure memory core power:
        if self.memory_technology == "SRAM":
            self.power_sram_read = _powersramread(2**self.row_decoder_bits, 2**self.col_decoder_bits)
            self.power_sram_writelh = _powersramwritelh(2**self.row_decoder_bits, 2**self.col_decoder_bits)
            self.power_sram_writehh = _powersramwritehh(2**self.row_decoder_bits, 2**self.col_decoder_bits)
            self.power_sram_writep = _powersramwritep(2**self.row_decoder_bits, 2**self.col_decoder_bits)

        elif self.memory_technology == "MTJ":
            self.power_mtj_write = _powermtjwrite(2**self.row_decoder_bits)
            self.power_mtj_read = _powermtjread(2**self.row_decoder_bits)


        # Create precharge and equalization
        if self.memory_technology == "SRAM":
            self.precharge = _prechargeandeq(self.use_tgate, 2**self.row_decoder_bits)
            self.samp_part2 = _samp_part2(self.use_tgate, 2**self.row_decoder_bits, 0.3)
            self.samp = _samp(self.use_tgate, 2**self.row_decoder_bits, 0, 0.3)
            self.writedriver = _writedriver(self.use_tgate, 2**self.row_decoder_bits)

        elif self.memory_technology == "MTJ":
            self.mtjbasics = _mtjbasiccircuits()
            self.bldischarging = _mtjbldischarging(2**self.row_decoder_bits)
            self.blcharging = _mtjblcharging(2**self.row_decoder_bits)
            self.mtjsamp = _mtjsamp(2**self.row_decoder_bits)


        self.columndecoder = _columndecoder(self.use_tgate, 2**(self.col_decoder_bits + self.conf_decoder_bits), self.col_decoder_bits)
        #create the level shifter

        self.levelshift = _levelshifter()
        #the configurable decoder:


        self.cpredecoder = 1
        self.cpredecoder1 = 0
        self.cpredecoder2 = 0
        self.cpredecoder3 = 0

        assert self.conf_decoder_bits >= 4
        assert self.conf_decoder_bits <= 9

        # Same as the row decoder
        #determine decoder object sizes
        if self.conf_decoder_bits == 4:
            self.cpredecoder = 4
            self.cpredecoder1 = 2
            self.cpredecoder2 = 2  
        if self.conf_decoder_bits == 5:
            self.cpredecoder = 5
            self.cpredecoder1 = 3
            self.cpredecoder2 = 2
        if self.conf_decoder_bits == 6:
            self.cpredecoder = 6
            self.cpredecoder1 = 3
            self.cpredecoder2 = 3
        if self.conf_decoder_bits == 7:
            self.cpredecoder = 7
            self.cpredecoder1 = 2
            self.cpredecoder2 = 2
            self.cpredecoder3 = 3
        if self.conf_decoder_bits == 8:
            self.cpredecoder = 8
            self.cpredecoder1 = 3
            self.cpredecoder2 = 3
            self.cpredecoder3 = 2
        if self.conf_decoder_bits == 9:
            self.cpredecoder = 9
            self.cpredecoder1 = 3
            self.cpredecoder2 = 3
            self.cpredecoder3 = 3

        self.cfanouttypeconf = 2
        if self.cpredecoder3 != 0:
            self.cfanouttypeconf = 3

        self.cfanin1 = 0
        self.cfanin2 = 0
        self.cvalidobj1 = 0
        self.cvalidobj2 = 0

        self.stage1output3 = 0
        self.stage1output2 = 0

        if self.cpredecoder1 == 3:
            self.stage1output3+=8
        if self.cpredecoder2 == 3:
            self.stage1output3+=8 
        if self.cpredecoder3 == 3:
            self.stage1output3+=8

        if self.cpredecoder1 == 2:
            self.stage1output2+=4
        if self.cpredecoder2 == 2:
            self.stage1output2+=4 
        if self.cpredecoder3 == 2:
            self.stage1output2+=4              

        if self.cpredecoder1 == 3 or self.cpredecoder2 == 3 or self.cpredecoder3 == 3:
            self.configurabledecoder3ii =  _configurabledecoder3ii(use_tgate, 3, 2**(self.cpredecoder1+self.cpredecoder1+self.cpredecoder1 - 3), self.cfanouttypeconf, self.stage1output3)
            self.cfanin1 = 2**(self.cpredecoder1+self.cpredecoder2+self.cpredecoder3 - 3)
            self.cvalidobj1 = 1

        if self.cpredecoder2 == 2 or self.cpredecoder2 == 2 or self.cpredecoder3 == 2:
            self.configurabledecoder2ii =  _configurabledecoder2ii(use_tgate, 2, 2**(self.cpredecoder1+self.cpredecoder1+self.cpredecoder1 - 2), self.cfanouttypeconf, self.stage1output2)
            self.cfanin2 = 2**(self.cpredecoder1+self.cpredecoder2+self.cpredecoder3 - 2)
            self.cvalidobj2 = 1

        self.configurabledecoderi = _configurabledecoderinvmux(use_tgate, self.stage1output2/2, self.stage1output3/2, self.conf_decoder_bits)
        self.configurabledecoderiii = _configurabledecoderiii(use_tgate, self.cfanouttypeconf , self.cfanin1 , self.cfanin2, 2**self.conf_decoder_bits)

        self.pgateoutputcrossbar = _pgateoutputcrossbar(2**self.conf_decoder_bits)
        
    def generate(self, subcircuits_filename, min_tran_width, specs):
        print "Generating RAM block"
        init_tran_sizes = {}
        init_tran_sizes.update(self.RAM_local_mux.generate(subcircuits_filename, min_tran_width))

        if self.valid_row_dec_size2 == 1:
            init_tran_sizes.update(self.rowdecoder_stage1_size2.generate(subcircuits_filename, min_tran_width))

        if self.valid_row_dec_size3 == 1:
            init_tran_sizes.update(self.rowdecoder_stage1_size3.generate(subcircuits_filename, min_tran_width))

        init_tran_sizes.update(self.rowdecoder_stage3.generate(subcircuits_filename, min_tran_width))
        
        init_tran_sizes.update(self.rowdecoder_stage0.generate(subcircuits_filename, min_tran_width))
        init_tran_sizes.update(self.wordlinedriver.generate(subcircuits_filename, min_tran_width))
        self.RAM_local_routing_wire_load.generate(subcircuits_filename, specs, self.RAM_local_mux)

        self.memorycells.generate(subcircuits_filename, min_tran_width)

        if self.memory_technology == "SRAM":
            init_tran_sizes.update(self.precharge.generate(subcircuits_filename, min_tran_width))
            self.samp.generate(subcircuits_filename, min_tran_width)
            init_tran_sizes.update(self.writedriver.generate(subcircuits_filename, min_tran_width))
        else:
            init_tran_sizes.update(self.bldischarging.generate(subcircuits_filename, min_tran_width))
            init_tran_sizes.update(self.mtjbasics.generate(subcircuits_filename))


        init_tran_sizes.update(self.levelshift.generate(subcircuits_filename))
        
        init_tran_sizes.update(self.columndecoder.generate(subcircuits_filename, min_tran_width))

        init_tran_sizes.update(self.configurabledecoderi.generate(subcircuits_filename, min_tran_width))

        init_tran_sizes.update(self.configurabledecoderiii.generate(subcircuits_filename, min_tran_width))

        
        if self.cvalidobj1 == 1:
            init_tran_sizes.update(self.configurabledecoder3ii.generate(subcircuits_filename, min_tran_width))
        if self.cvalidobj2 == 1:
            init_tran_sizes.update(self.configurabledecoder2ii.generate(subcircuits_filename, min_tran_width))


        init_tran_sizes.update(self.pgateoutputcrossbar.generate(subcircuits_filename, min_tran_width))

        return init_tran_sizes

        
    def _update_process_data(self):
        """ I'm using this file to update several timing variables after measuring them. """
        
        process_data_file = open(self.process_data_filename, 'w')
        process_data_file.write("*** PROCESS DATA AND VOLTAGE LEVELS\n\n")
        process_data_file.write(".LIB PROCESS_DATA\n\n")
        process_data_file.write("* Voltage levels\n")
        process_data_file.write(".PARAM supply_v = " + str(self.cspecs.vdd) + "\n")
        process_data_file.write(".PARAM sram_v = " + str(self.cspecs.vsram) + "\n")
        process_data_file.write(".PARAM sram_n_v = " + str(self.cspecs.vsram_n) + "\n")
        process_data_file.write(".PARAM Rcurrent = " + str(self.cspecs.worst_read_current) + "\n")
        process_data_file.write(".PARAM supply_v_lp = " + str(self.cspecs.vdd_low_power) + "\n\n")


        if use_lp_transistor == 0 :
            process_data_file.write(".PARAM sense_v = " + str(self.cspecs.vdd - self.cspecs.sense_dv) + "\n\n")
        else:
            process_data_file.write(".PARAM sense_v = " + str(self.cspecs.vdd_low_power - self.cspecs.sense_dv) + "\n\n")


        process_data_file.write(".PARAM mtj_worst_high = " + str(self.cspecs.MTJ_Rhigh_worstcase) + "\n")
        process_data_file.write(".PARAM mtj_worst_low = " + str(self.cspecs.MTJ_Rlow_worstcase) + "\n")
        process_data_file.write(".PARAM mtj_nominal_low = " + str(self.cspecs.MTJ_Rlow_nominal) + "\n\n")
        process_data_file.write(".PARAM mtj_nominal_high = " + str(6250) + "\n\n") 
        process_data_file.write(".PARAM vref = " + str(self.cspecs.vref) + "\n")
        process_data_file.write(".PARAM vclmp = " + str(self.cspecs.vclmp) + "\n")

        process_data_file.write("* Misc parameters\n")

        process_data_file.write(".PARAM ram_frequency = " + str(self.frequency) + "\n")
        process_data_file.write(".PARAM precharge_max = " + str(self.T1) + "\n")
        if self.cspecs.memory_technology == "SRAM":
            process_data_file.write(".PARAM wl_eva = " + str(self.T1 + self.T2) + "\n")
            process_data_file.write(".PARAM sa_xbar_ff = " + str(self.frequency) + "\n")
        elif self.cspecs.memory_technology == "MTJ":
            process_data_file.write(".PARAM target_bl = " + str(self.target_bl) + "\n")
            process_data_file.write(".PARAM time_bl = " + str(self.blcharging.delay) + "\n")
            process_data_file.write(".PARAM sa_se1 = " + str(self.T2) + "\n")
            process_data_file.write(".PARAM sa_se2 = " + str(self.T3) + "\n")

        process_data_file.write("* Geometry\n")
        process_data_file.write(".PARAM gate_length = " + str(self.cspecs.gate_length) + "n\n")
        process_data_file.write(".PARAM trans_diffusion_length = " + str(self.cspecs.trans_diffusion_length) + "n\n")
        process_data_file.write(".PARAM min_tran_width = " + str(self.cspecs.min_tran_width) + "n\n")
        process_data_file.write(".param rest_length_factor=" + str(self.cspecs.rest_length_factor) + "\n")
        process_data_file.write("\n")

        process_data_file.write("* Supply voltage.\n")
        process_data_file.write("VSUPPLY vdd gnd supply_v\n")
        process_data_file.write("VSUPPLYLP vdd_lp gnd supply_v_lp\n")
        process_data_file.write("* SRAM voltages connecting to gates\n")
        process_data_file.write("VSRAM vsram gnd sram_v\n")
        process_data_file.write("VrefMTJn vrefmtj gnd vref\n")
        process_data_file.write("Vclmomtjn vclmpmtj gnd vclmp\n")
        process_data_file.write("VSRAM_N vsram_n gnd sram_n_v\n\n")
        process_data_file.write("* Device models\n")
        process_data_file.write(".LIB \"" + self.cspecs.model_path + "\" " + self.cspecs.model_library + "\n\n")
        process_data_file.write(".ENDL PROCESS_DATA")
        process_data_file.close()
        

    def generate_top(self):

        # Generate top-level evaluation paths for all components:

        self.RAM_local_mux.generate_top()

        self.rowdecoder_stage0.generate_top()


        self.rowdecoder_stage3.generate_top()

        if self.valid_row_dec_size2 == 1:
            self.rowdecoder_stage1_size2.generate_top()
        if self.valid_row_dec_size3 == 1:
            self.rowdecoder_stage1_size3.generate_top()


        if self.memory_technology == "SRAM":
            self.precharge.generate_top()
            self.samp_part2.generate_top()
            self.samp.generate_top()
            self.writedriver.generate_top()
            self.power_sram_read.generate_top()
            self.power_sram_writelh.generate_top()
            self.power_sram_writehh.generate_top()
            self.power_sram_writep.generate_top()
        else:
            self.bldischarging.generate_top()
            self.blcharging.generate_top()
            self.mtjsamp.generate_top()
            self.power_mtj_write.generate_top()
            self.power_mtj_read.generate_top()

        self.columndecoder.generate_top()
        self.configurabledecoderi.generate_top()
        self.configurabledecoderiii.generate_top()

        
        if self.cvalidobj1 == 1:
            self.configurabledecoder3ii.generate_top()
        if self.cvalidobj2 == 1:
            self.configurabledecoder2ii.generate_top()

        self.pgateoutputcrossbar.generate_top()
        self.wordlinedriver.generate_top()

    def update_area(self, area_dict, width_dict):

        
        self.RAM_local_mux.update_area(area_dict, width_dict)

        self.rowdecoder_stage0.update_area(area_dict, width_dict) 

        self.rowdecoder_stage3.update_area(area_dict, width_dict)

        if self.valid_row_dec_size2 == 1:
            self.rowdecoder_stage1_size2.update_area(area_dict, width_dict)
        if self.valid_row_dec_size3 == 1:
            self.rowdecoder_stage1_size3.update_area(area_dict, width_dict)

        self.memorycells.update_area(area_dict, width_dict)

        if self.memory_technology == "SRAM":
            self.precharge.update_area(area_dict, width_dict)
            self.samp.update_area(area_dict, width_dict)
            self.writedriver.update_area(area_dict, width_dict)
        else:
            self.mtjbasics.update_area(area_dict, width_dict)

        self.columndecoder.update_area(area_dict, width_dict)
        self.configurabledecoderi.update_area(area_dict, width_dict)
        if self.cvalidobj1 == 1:
            self.configurabledecoder3ii.update_area(area_dict, width_dict)
        if self.cvalidobj2 == 1:    
            self.configurabledecoder2ii.update_area(area_dict, width_dict)

        self.configurabledecoderiii.update_area(area_dict, width_dict)
        self.pgateoutputcrossbar.update_area(area_dict, width_dict)
        self.wordlinedriver.update_area(area_dict, width_dict)
        self.levelshift.update_area(area_dict, width_dict)
    
        
    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wires of things inside the RAM block. """
        
        self.RAM_local_mux.update_wires(width_dict, wire_lengths, wire_layers)
        self.RAM_local_routing_wire_load.update_wires(width_dict, wire_lengths, wire_layers)
        self.rowdecoder_stage0.update_wires(width_dict, wire_lengths, wire_layers)
        self.memorycells.update_wires(width_dict, wire_lengths, wire_layers)
        self.wordlinedriver.update_wires(width_dict, wire_lengths, wire_layers)
        self.configurabledecoderi.update_wires(width_dict, wire_lengths, wire_layers)
        self.pgateoutputcrossbar.update_wires(width_dict, wire_lengths, wire_layers)
        self.configurabledecoderiii.update_wires(width_dict, wire_lengths, wire_layers)

        
        
    def print_details(self, report_file):
        self.RAM_local_mux.print_details(report_file)




class _HBLocalMUX(_SizableCircuit):
    """ Hard block Local MUX Class: Pass-transistor 2-level mux with driver """
    
    def __init__(self, required_size, num_per_tile, use_tgate, hb_parameters):
        
        self.hb_parameters = hb_parameters
        # Subcircuit name
        self.name = hb_parameters['name'] + "_local_mux"
        # How big should this mux be (dictated by architecture specs)
        self.required_size = required_size 
        # How big did we make the mux (it is possible that we had to make the mux bigger for level sizes to work out, this is how big the mux turned out)
        self.implemented_size = -1
        # This is simply the implemented_size-required_size
        self.num_unused_inputs = -1
        # Number of switch block muxes in one FPGA tile
        self.num_per_tile = num_per_tile
        # Number of SRAM cells per mux
        self.sram_per_mux = -1
        # Size of the first level of muxing
        self.level1_size = -1
        # Size of the second level of muxing
        self.level2_size = -1
        # Delay weight in a representative critical path
        self.delay_weight = DELAY_WEIGHT_RAM
        # use pass transistor or transmission gates
        self.use_tgate = use_tgate
    
    
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating HB local mux"
        
        # Calculate level sizes and number of SRAMs per mux
        self.level2_size = int(math.sqrt(self.required_size))
        self.level1_size = int(math.ceil(float(self.required_size)/self.level2_size))
        self.implemented_size = self.level1_size*self.level2_size
        self.num_unused_inputs = self.implemented_size - self.required_size
        self.sram_per_mux = self.level1_size + self.level2_size
        
        if not self.use_tgate :
            # Call generation function
            self.transistor_names, self.wire_names = mux_subcircuits.generate_ptran_2lvl_mux(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["ptran_" + self.name + "_L1_nmos"] = 2
            self.initial_transistor_sizes["ptran_" + self.name + "_L2_nmos"] = 2
            self.initial_transistor_sizes["rest_" + self.name + "_pmos"] = 1
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 6
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 12
        else :
            # Call MUX generation function
            self.transistor_names, self.wire_names = mux_subcircuits.generate_tgate_2lvl_mux(subcircuit_filename, self.name, self.level1_size, self.level2_size)
            
            # Initialize transistor sizes (to something more reasonable than all min size, but not necessarily a good choice, depends on architecture params)
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_nmos"] = 3
            self.initial_transistor_sizes["tgate_" + self.name + "_L1_pmos"] = 3
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_nmos"] = 4
            self.initial_transistor_sizes["tgate_" + self.name + "_L2_pmos"] = 4
            self.initial_transistor_sizes["inv_" + self.name + "_1_nmos"] = 8
            self.initial_transistor_sizes["inv_" + self.name + "_1_pmos"] = 4
            self.initial_transistor_sizes["inv_" + self.name + "_2_nmos"] = 10
            self.initial_transistor_sizes["inv_" + self.name + "_2_pmos"] = 20

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level HB local mux"

        self.top_spice_path = top_level.generate_HB_local_mux_top(self.name, self.hb_parameters['name'])

   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # MUX area
        if not self.use_tgate :
            area = ((self.level1_size*self.level2_size)*area_dict["ptran_" + self.name + "_L1"] +
                    self.level2_size*area_dict["ptran_" + self.name + "_L2"] +
                    area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"])
        else :
            area = ((self.level1_size*self.level2_size)*area_dict["tgate_" + self.name + "_L1"] +
                    self.level2_size*area_dict["tgate_" + self.name + "_L2"] +
                    # area_dict["rest_" + self.name + ""] +
                    area_dict["inv_" + self.name + "_1"])
          
        # MUX area including SRAM
        area_with_sram = (area + (self.level1_size + self.level2_size)*area_dict["sram"])

        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        self.area = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram



    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths
        wire_lengths["wire_" + self.name + "_driver"] = (width_dict["inv_" + self.name + "_1"] + width_dict["inv_" + self.name + "_2"])/4
        wire_lengths["wire_" + self.name + "_L1"] = width_dict[self.name]
        wire_lengths["wire_" + self.name + "_L2"] = width_dict[self.name]
        
        # Update wire layers
        wire_layers["wire_" + self.name + "_driver"] = 0
        wire_layers["wire_" + self.name + "_L1"] = 0
        wire_layers["wire_" + self.name + "_L2"] = 0  



class _HBLocalRoutingWireLoad:
    """ Hard Block Local routing wire load """
    
    def __init__(self, hb_parameters):
        self.hb_parameters = hb_parameters
        # Name of this wire
        self.name = hb_parameters['name'] + "_local_routing_wire_load"
        # This is obtained from the user)
        self.RAM_input_usage_assumption = hb_parameters['input_usage']
        # Total number of local mux inputs per wire
        self.mux_inputs_per_wire = -1
        # Number of on inputs connected to each wire 
        self.on_inputs_per_wire = -1
        # Number of partially on inputs connected to each wire
        self.partial_inputs_per_wire = -1
        #Number of off inputs connected to each wire
        self.off_inputs_per_wire = -1
        # List of wire names in the SPICE circuit
        self.wire_names = []


    def generate(self, subcircuit_filename, specs, HB_local_mux):
        print "Generating local routing wire load"
        # Compute load (number of on/partial/off per wire)
        self._compute_load(specs, HB_local_mux)
        # Generate SPICE deck
        self.wire_names = load_subcircuits.hb_local_routing_load_generate(subcircuit_filename, self.on_inputs_per_wire, self.partial_inputs_per_wire, self.off_inputs_per_wire, self.name, HB_local_mux.name)
    
    
    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """
        
        # Update wire lengths
        wire_lengths["wire_"+self.hb_parameters['name']+"_local_routing"] = width_dict[self.hb_parameters['name']]
        # Update wire layers
        wire_layers["wire_"+self.hb_parameters['name']+"_local_routing"] = 0
    
        
    def _compute_load(self, specs, HB_local_mux):
        """ Compute the load on a local routing wire (number of on/partial/off) """
        
        # The first thing we are going to compute is how many local mux inputs are connected to a local routing wire
        # FOR a ram block its the number of inputs
        num_local_routing_wires = self.hb_parameters['num_gen_inputs']
        self.mux_inputs_per_wire = HB_local_mux.implemented_size
        
        # Now we compute how many "on" inputs are connected to each routing wire
        # Right now I assume the hard block doesn't have any local feedbacks. If that's the case the following line should be changed
        num_local_muxes_used = self.RAM_input_usage_assumption*self.hb_parameters['num_gen_inputs']
        self.on_inputs_per_wire = int(num_local_muxes_used/num_local_routing_wires)
        # We want to model for the case where at least one "on" input is connected to the local wire, so make sure it's at least 1
        if self.on_inputs_per_wire < 1:
            self.on_inputs_per_wire = 1
        
        # Now we compute how many partially on muxes are connected to each wire
        # The number of partially on muxes is equal to (level2_size - 1)*num_local_muxes_used/num_local_routing_wire
        # We can figure out the number of muxes used by using the "on" assumption and the number of local routing wires.
        self.partial_inputs_per_wire = int((HB_local_mux.level2_size - 1.0)*num_local_muxes_used/num_local_routing_wires)
        # Make it at least 1
        if self.partial_inputs_per_wire < 1:
            self.partial_inputs_per_wire = 1
        
        # Number of off inputs is simply the difference
        self.off_inputs_per_wire = self.mux_inputs_per_wire - self.on_inputs_per_wire - self.partial_inputs_per_wire



# We need four classes for a hard block
# The high-level, the input crossbar, and possibly dedicated routing links and the local routing wireload
class _dedicated_routing_driver(_SizableCircuit):
    """ dedicated routing driver class"""

    def __init__(self, name, top_name, num_buffers):

        # Subcircuit name
        self.name = name
        # hard block name
        self.top_name = top_name
        
        self.num_buffers = num_buffers
    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating hard block " + self.name +" dedicated routing driver"

        self.transistor_names, self.wire_names = mux_subcircuits.generate_dedicated_driver(subcircuit_filename, self.name, self.num_buffers, self.top_name)
            
        for i in range(1, self.num_buffers * 2 + 1):
            self.initial_transistor_sizes["inv_" + self.name + "_"+str(i)+"_nmos"] = 2
            self.initial_transistor_sizes["inv_" + self.name + "_"+str(i)+"_pmos"] = 2
  

        return self.initial_transistor_sizes

    def generate_top(self):
        print "Generating top-level submodules"

        self.top_spice_path = top_level.generate_dedicated_driver_top(self.name, self.top_name, self.num_buffers)

   
    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        

        area = 0.0
        for i in range(1, self.num_buffers * 2 + 1):
            area += area_dict["inv_" + self.name +"_"+ str(i) ]

        area_with_sram = area
        self.area = area

        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram


class _hard_block(_CompoundCircuit):
    """ hard block class"""

    def __init__(self, filename, use_tgate):
        #Call the hard block parameter parser
        self.parameters = utils.load_hard_params(filename)
        # Subcircuit name
        self.name = self.parameters['name']
        #create the inner objects
        self.mux = _HBLocalMUX(int(math.ceil(self.parameters['num_gen_inputs']/self.parameters['num_crossbars'] * self.parameters['crossbar_population'])), self.parameters['num_gen_inputs'], use_tgate, self.parameters)
        self.load = _HBLocalRoutingWireLoad(self.parameters)
        if self.parameters['num_dedicated_outputs'] > 0:
            # the user can change this line to add more buffers to their dedicated links. In my case 2 will do.
            self.dedicated =_dedicated_routing_driver(self.name + "_ddriver", self.name, 2)

        self.flow_results = (-1.0,-1.0,-1.0)

    def generate(self, subcircuit_filename, min_tran_width):
        print "Generating hard block " + self.name

        # generate subblocks
        init_tran_sizes = {}
        init_tran_sizes.update(self.mux.generate(subcircuit_filename, min_tran_width))
        if self.parameters['num_dedicated_outputs'] > 0:
            init_tran_sizes.update(self.dedicated.generate(subcircuit_filename, min_tran_width))
        # wireload
        self.load.generate(subcircuit_filename, min_tran_width, self.mux)

        return init_tran_sizes

    def generate_top(self):

        print "Generating top-level submodules"

        self.mux.generate_top()
        if self.parameters['num_dedicated_outputs'] > 0:
            self.dedicated.generate_top()

        # hard flow
        self.flow_results = hardblock_functions.hardblock_flow(self.parameters)
        self.area = self.flow_results[0] * self.parameters['area_scale_factor'] * 1e+6

        self.mux.lowerbounddelay = self.flow_results[1] * (1.0/self.parameters['freq_scale_factor']) * 1e-9
		
        if self.parameters['num_dedicated_outputs'] > 0:
            self.dedicated.lowerbounddelay = self.flow_results[1] * (1.0/self.parameters['freq_scale_factor']) * 1e-9

    def update_area(self, area_dict, width_dict):
        """ Update area. To do this, we use area_dict which is a dictionary, maintained externally, that contains
            the area of everything. It is expected that area_dict will have all the information we need to calculate area.
            We update area_dict and width_dict with calculations performed in this function. """        
        
        # update the area of subblocks:
        self.mux.update_area(area_dict, width_dict)

        if self.parameters['num_dedicated_outputs'] > 0:
            self.dedicated.update_area(area_dict, width_dict) 

        # area of the block itself
        if self.parameters['num_dedicated_outputs'] > 0:
            area = self.parameters['num_dedicated_outputs'] * area_dict[self.name + "_ddriver"] + self.parameters['num_gen_inputs'] * area_dict[self.mux.name] + self.area
        else :
            area = self.parameters['num_gen_inputs'] * area_dict[self.mux.name] + self.area

        area_with_sram = area
        width = math.sqrt(area)
        width_with_sram = math.sqrt(area_with_sram)
        area_dict[self.name] = area
        width_dict[self.name] = width
        area_dict[self.name + "_sram"] = area_with_sram
        width_dict[self.name + "_sram"] = width_with_sram



    def update_wires(self, width_dict, wire_lengths, wire_layers):
        """ Update wire lengths and wire layers based on the width of things, obtained from width_dict. """

        # Update wire lengths
        wire_lengths["wire_" + self.name + "_1"] = width_dict[self.name]
        wire_lengths["wire_" + self.name + "_2"] = width_dict["tile"] * self.parameters['soft_logic_per_block']
        wire_lengths["wire_" + self.name + "_local_routing_wire_load"] = width_dict[self.name]
        
        # Update wire layers
        wire_layers["wire_" + self.name + "_1"] = 0
        wire_layers["wire_" + self.name + "_2"] = 1  
        wire_layers["wire_" + self.name + "_local_routing_wire_load"] = 0





  
class FPGA:
    """ This class describes an FPGA. """
        
    def __init__(self, arch_params_dict, run_options, spice_interface):
          
        # Initialize the specs
        self.specs = _Specs(arch_params_dict, run_options.quick_mode)


        ######################################
        ### INITIALIZE SPICE LIBRARY NAMES ###
        ######################################

        self.wire_RC_filename           = "wire_RC.l"
        self.process_data_filename      = "process_data.l"
        self.includes_filename          = "includes.l"
        self.basic_subcircuits_filename = "basic_subcircuits.l"
        self.subcircuits_filename       = "subcircuits.l"
        self.sweep_data_filename        = "sweep_data.l"

        # this is a temporary architecture parameter used to indicated that we need to 
        # apply the new architecutre being developed
        self.updates = arch_params_dict['updates']

                                        
        ##################################
        ### CREATE SWITCH BLOCK OBJECT ###
        ##################################

        # Calculate switch block mux size (for direct-drive routing)
        # The mux will need Fs + (Fs-1)(L-1) inputs for routing-to-routing connections
        # The Fs term comes from starting wires, the (Fs-1) term comes from non-starting wires, of which there are (L-1)
        r_to_r_sb_mux_size = self.specs.Fs + (self.specs.Fs-1)*(self.specs.L-1)
        # Then, each mux needs No*Fcout*L/2 additional inputs for logic cluster outputs (No = number of cluster outputs (Or))
        No = self.specs.num_cluster_outputs
        clb_to_r_sb_mux_size = No*self.specs.Fcout*self.specs.L/2
        sb_mux_size_required = int(r_to_r_sb_mux_size + clb_to_r_sb_mux_size)
        # Calculate number of switch block muxes per tile
        num_sb_mux_per_tile = 2*self.specs.W/self.specs.L
        # Initialize the switch block
        self.sb_mux = _SwitchBlockMUX(sb_mux_size_required, num_sb_mux_per_tile, self.specs.use_tgate)

        
        ######################################
        ### CREATE CONNECTION BLOCK OBJECT ###
        ######################################

        # Calculate connection block mux size
        # Size is W*Fcin
        cb_mux_size_required = int(self.specs.W*self.specs.Fcin)
        num_cb_mux_per_tile = self.specs.I
        # Initialize the connection block
        self.cb_mux = _ConnectionBlockMUX(cb_mux_size_required, num_cb_mux_per_tile, self.specs.use_tgate)

        
        ###################################
        ### CREATE LOGIC CLUSTER OBJECT ###
        ###################################

        # new design doesn't have local feedback
        if self.updates:
            self.specs.num_ble_local_outputs = 0

        # Calculate local mux size
        # Local mux size is (inputs + feedback) * population
        local_mux_size_required = int((self.specs.I + self.specs.num_ble_local_outputs*self.specs.N) * self.specs.Fclocal)
        num_local_mux_per_tile = self.specs.N*(self.specs.K+self.specs.independent_inputs)

        inter_wire_length = 0.5
        # Todo: make this a parameter
        self.skip_size = 5
        self.carry_skip_periphery_count = 0
        if self.specs.enable_carry_chain == 1 and self.specs.carry_chain_type == "skip":
            self.carry_skip_periphery_count = int(math.floor((self.specs.N * self.specs.FAs_per_flut)/self.skip_size))
        # initialize the logic cluster
        self.logic_cluster = _LogicCluster(self.specs, self.carry_skip_periphery_count, local_mux_size_required, num_local_mux_per_tile)
        
        ###########################
        ### CREATE LOAD OBJECTS ###
        ###########################

        # Create cluster output load object
        self.cluster_output_load = _GeneralBLEOutputLoad()
        # Create routing wire load object
        self.routing_wire_load = _RoutingWireLoad(self.specs.L)


        ##################################
        ### CREATE CARRY CHAIN OBJECTS ###
        ##################################
        # TODO: Why is the carry chain created here and not in the ble object?

        if self.specs.enable_carry_chain == 1:
            self.carrychain = _CarryChain(self.specs.use_finfet, self.specs.carry_chain_type, self.specs.N, self.specs.FAs_per_flut, self.updates)
            self.carrychainperf = _CarryChainPer(self.specs.use_finfet, self.specs.carry_chain_type, self.specs.N, self.specs.FAs_per_flut, self.specs.use_tgate, self.updates)
            if not self.updates:
                self.carrychainmux = _CarryChainMux(self.specs.use_finfet, self.specs.use_fluts, self.specs.use_tgate)
            self.carrychaininter = _CarryChainInterCluster(self.specs.use_finfet, self.specs.carry_chain_type, inter_wire_length, self.updates)
            if self.specs.carry_chain_type == "skip":
                self.carrychainand = _CarryChainSkipAnd(self.specs.use_finfet, self.specs.use_tgate, self.specs.carry_chain_type, self.specs.N, self.specs.FAs_per_flut, self.skip_size)
                self.carrychainskipmux = _CarryChainSkipMux(self.specs.use_finfet, self.specs.carry_chain_type, self.specs.use_tgate)

        ####################################
        ### CREATE CARRY CHAIN 2 OBJECTS ###
        ####################################

        # IMPORTANT: this should be always created after the fist carry chain, since its internal name has to do with this oder

        if self.updates in (2, 3):
            self.carrychain2 = _CarryChain(self.specs.use_finfet, self.specs.carry_chain_type, self.specs.N, self.specs.FAs_per_flut, self.updates)
            self.carrychainperf2 = _CarryChainPer(self.specs.use_finfet, self.specs.carry_chain_type, self.specs.N, self.specs.FAs_per_flut, self.specs.use_tgate, self.updates)
            self.carrychaininter2 = _CarryChainInterCluster(self.specs.use_finfet, self.specs.carry_chain_type, inter_wire_length, self.updates)

        
        #########################
        ### CREATE RAM OBJECT ###
        #########################

        RAM_local_mux_size_required = float(self.specs.ram_local_mux_size)
        RAM_num_mux_per_tile = (3 + 2*(self.specs.row_decoder_bits + self.specs.col_decoder_bits + self.specs.conf_decoder_bits ) + 2** (self.specs.conf_decoder_bits))
        self.RAM = _RAM(self.specs.row_decoder_bits, self.specs.col_decoder_bits, self.specs.conf_decoder_bits, RAM_local_mux_size_required, 
                        RAM_num_mux_per_tile , self.specs.use_tgate, self.specs.sram_cell_area*self.specs.min_width_tran_area, self.specs.number_of_banks,
                        self.specs.memory_technology, self.specs, self.process_data_filename, self.specs.read_to_write_ratio)
        self.number_of_banks = self.specs.number_of_banks

        
        ################################
        ### CREATE HARD BLOCK OBJECT ###
        ################################

        self.hardblocklist = []
        self.hard_block_files = self.specs.hb_files
        #self.hard_block_files = {}
        for name in self.hard_block_files:
            hard_block = _hard_block(name, self.specs.use_tgate)
            self.hardblocklist.append(hard_block)


        ##########################################################
        ### INITIALIZE OTHER VARIABLES, LISTS AND DICTIONARIES ###
        ##########################################################


        self.area_opt_weight = run_options.area_opt_weight
        self.delay_opt_weight = run_options.delay_opt_weight
        self.spice_interface = spice_interface        
        # This is a dictionary of all the transistor sizes in the FPGA ('name': 'size')
        # It will contain the data in xMin transistor width, e.g. 'inv_sb_mux_1_nmos': '2'
        # That means inv_sb_mux_1_nmos is a transistor with 2x minimum width
        self.transistor_sizes = {}
        # This is a list of tuples containing area information for each transistor in the FPGA
        # Tuple: (tran_name, tran_channel_width_nm, tran_drive_strength, tran_area_min_areas, tran_area_nm, tran_width_nm)
        self.transistor_area_list = []
        
        # A note on the following 5 dictionaries
        # (area_dict, width_dict, wire_lengths, wire_layers, wire_rc_dict)
        #
        # Transistor sizes and wire lengths are needed at many different places in the SPICE netlists
        # that COFFE creates (e.g. the size of a particular transistor might be needed in many 
        # different files or multiple times in the same file). Since it would be a pain to have to 
        # go through every single line in every single file each time we want to change the size of 
        # a transistor (which will happen many thousands of times), COFFE inserts variables in the
        # SPICE netlists that it creates. These variables, which describe transistor sizes and wire 
        # loads, are assigned values in external files (one file for transistor sizes, one for wire loads). 
        # That way, when we change the size of a transistor (or a wire load), we only need to change
        # it in one place, and this change is seen by all SPICE netlists. 
        # The data structures that COFFE uses to keep track of transistor/circuit areas and wire data 
        # use a similar philosophy. That is, the following 5 dictionaries contain information about 
        # all element in the FPGA (all in one place). For ex., if we want to know the area of a switch block
        # multiplexer we ask 'area_dict' (e.g. area_dict['sb_mux']). One of the reasons for doing this
        # is that it makes outputing this data easier. For example, when we want to update that 'wire
        # load file' that the SPICE netlists use, all we need to do is write out wire_rc_dict to that file.
        # But, the 'fpga' object does not know how to update the area and wire data of each subcircuit.
        # Therefore, these dictionaries will be passed into member objects who will populate them as needed.
        # So, that's just something to keep in mind as you go through this code. You'll likely see these
        # dictionaries a lot.
        #
        # This is a dictionary that contains the area of everything for all levels of hierarchy in the FPGA. 
        # It has transistor area, inverter areas, mux areas, switch_block area, tile area.. etc. 
        # ('entity_name': area) All these areas are in nm^2
        self.area_dict = {}
        # This is a dictionary that contains the width of everything (much like area_dict has the areas).
        # ('entity_name': width) All widths are in nm. The width_dict is useful for figuring out wire lengths.
        self.width_dict = {}
        # This dictionary contains the lengths of all the wires in the FPGA. ('wire_name': length). Lengths in nm.
        self.wire_lengths = {}
        # This dictionary contains the metal layer for each wire. ('wire_name': layer)
        # The layer number (an int) is an index that will be used to select the right metal data
        # from the 'metal_stack' (list described below).
        self.wire_layers = {}
        # This dictionary contains wire resistance and capacitance for each wire as a tuple ('wire_name': (R, C))
        self.wire_rc_dict = {}
        
        # This dictionary contains the delays of all subcircuits (i.e. the max of rise and fall)
        # Contrary to the above 5 dicts, this one is not passed down into the other objects.
        # This dictionary is updated by calling 'update_delays()'
        self.delay_dict = {}
        
        # Metal stack. Lowest index is lowest metal layer. COFFE assumes that wire widths increase as we use higher metal layers.
        # For example, wires in metal_stack[1] are assumed to be wider (and/or more spaced) than wires in metal_stack[0]
        # e.g. metal_stack[0] = (R0, C0)
        self.metal_stack = self.specs.metal_stack
        
        # whether or not to use transmission gates
        self.use_tgate = self.specs.use_tgate

        # This is the height of the logic block, once an initial floorplanning solution has been determined, it will be assigned a non-zero value.
        self.lb_height =  0.0


        ########################################################################################
        ###  Add all subcircuits that needs delays calculations are added to the ordered     ###
        ###  dictionary. All the the subcircuits in the design except for the LUT, LUT       ###   
        ###  drivers, Hardblocks and RAMs should be added to this dictionary.                ###   
        ########################################################################################  

        # Subcircutis Ordered Dictionary
        self.subcircuits = collections.OrderedDict()

        # Switch Block Mux Subcircuit
        self.subcircuits[self.sb_mux.name] = self.sb_mux
        # Connection Block Mux Subcircuit
        self.subcircuits[self.cb_mux.name] = self.cb_mux
        # Local Interconnect Mux Subcircuit
        self.subcircuits[self.logic_cluster.local_mux.name] = self.logic_cluster.local_mux
        #JUNIUS - Adder Direct Local Interconnect Mux Subcircuit for LUT skip (mode 10)
        if self.updates == 10:
            self.subcircuits[self.logic_cluster.adder_direct_local_mux.name] = self.logic_cluster.adder_direct_local_mux
        # General Output Mux Subcircuit
        self.subcircuits[self.logic_cluster.ble.general_output.name] = self.logic_cluster.ble.general_output
        # LUT Subcircuit
        self.subcircuits[self.logic_cluster.ble.lut.name] = self.logic_cluster.ble.lut

        # Local Output Mux Subcircuit, not used in the Stratix10 design
        if not self.updates:
            self.subcircuits[self.logic_cluster.ble.local_output.name] = self.logic_cluster.ble.local_output

        # Level 1 FLUT Mux Subcircuit
        if self.specs.use_fluts:
            self.subcircuits[self.logic_cluster.ble.fmux.name] = self.logic_cluster.ble.fmux

        # Added subcircuits for the new design
        if self.updates:
            # General Output 3:1 Mux Subcircuit
            self.subcircuits[self.logic_cluster.ble.general_output3.name] = self.logic_cluster.ble.general_output3
            # Level 2 FLUT Mux Subcircuti
            self.subcircuits[self.logic_cluster.ble.fmux_l2.name] = self.logic_cluster.ble.fmux_l2

        # Add the third level of flut mux
        if self.updates == 4:
            self.subcircuits[self.logic_cluster.ble.fmux_l3.name] = self.logic_cluster.ble.fmux_l3

        # Carry Chain Subcircuits
        if self.specs.enable_carry_chain:
            #JUNIUS - add FLUT CC MUX for LUT skip (mode 10)
            if self.updates == 10:
                self.subcircuits[self.logic_cluster.ble.flut_cc_mux.name] = self.logic_cluster.ble.flut_cc_mux
            # Carry Chain Subcircuit, Cin to Cout
            self.subcircuits[self.carrychain.name] = self.carrychain
            # Carry Chain Pereferal Subcircuit, Cin to Sout
            self.subcircuits[self.carrychainperf.name] = self.carrychainperf
            # Carry Chain Inter Subcircuit, Cout to Cin (drivers between clusters)
            self.subcircuits[self.carrychaininter.name] = self.carrychaininter
            # Stratix10 design doesn't have this mux
            if not self.updates:
                # Carry Chain Mux Subcircuit (Mux choosing between lut output and carry chain output)
                self.subcircuits[self.carrychainmux.name] = self.carrychainmux
            if self.specs.carry_chain_type == "skip":
                self.subcircuits[self.carrychainskipmux.name] = self.carrychainskipmux
                self.subcircutis[self.carrychainand.name] = self.carrychainand   

        # Carry Chain 2 for the new architecure
        if self.updates in (2, 3):
            self.subcircuits[self.carrychain2.name] = self.carrychain2
            self.subcircuits[self.carrychainperf2.name] = self.carrychainperf2
            self.subcircuits[self.carrychaininter2.name] = self.carrychaininter2 

        # Add the input select mux of the ff to the circuits that needs delay calculations
        if self.updates in (1, 2, 4, 10):
            self.subcircuits[self.logic_cluster.ble.ff2.input_mux.name] = self.logic_cluster.ble.ff2.input_mux
            self.subcircuits[self.logic_cluster.ble.ff3.input_mux.name] = self.logic_cluster.ble.ff3.input_mux
        elif self.updates == 3:
            self.subcircuits[self.logic_cluster.ble.ff3.input_mux.name] = self.logic_cluster.ble.ff3.input_mux
            self.subcircuits[self.logic_cluster.ble.ff4.input_mux.name] = self.logic_cluster.ble.ff4.input_mux



    def generate(self):
        """ This function generates all SPICE netlists and library files. """
    
        # Here's a file-stack that shows how COFFE organizes its SPICE files.
        # We'll talk more about each one as we generate them below.
    
        # ---------------------------------------------------------------------------------
        # |                                                                               |
        # |                top-level spice files (e.g. sb_mux.sp)                         |
        # |                                                                               |
        # ---------------------------------------------------------------------------------
        # |                                                                               |
        # |                                includes.l                                     |
        # |                                                                               |
        # ---------------------------------------------------------------------------------
        # |                                                                               |
        # |                               subcircuits.l                                   |
        # |                                                                               |
        # ---------------------------------------------------------------------------------
        # |                         |                               |                     |
        # |     process_data.l      |     basic_subcircuits.l       |     sweep_data.l    |
        # |                         |                               |                     |
        # ---------------------------------------------------------------------------------
    
        
        # Generate basic subcircuit library (pass-transistor, inverter, wire, etc.).
        # This library will be used to build other netlists.
        self._generate_basic_subcircuits()
        
        # Create 'subcircuits.l' library.
        # The subcircuit generation functions between 'self._create_lib_files()'
        # and 'self._end_lib_files()' will add things to these library files. 
        self._create_lib_files()
        
        # Generate the various subcircuits netlists of the FPGA (call members)
        self.transistor_sizes.update(self.sb_mux.generate(self.subcircuits_filename))                                        
        self.transistor_sizes.update(self.cb_mux.generate(self.subcircuits_filename)) 
                                                          
        self.transistor_sizes.update(self.logic_cluster.generate(self.subcircuits_filename, self.specs))
        self.cluster_output_load.generate(self.subcircuits_filename, self.specs, self.sb_mux)
        self.routing_wire_load.generate(self.subcircuits_filename, self.specs, self.sb_mux, self.cb_mux)

        if self.specs.enable_carry_chain == 1:
            self.transistor_sizes.update(self.carrychain.generate(self.subcircuits_filename))
            self.transistor_sizes.update(self.carrychainperf.generate(self.subcircuits_filename))
            if not self.updates:
                self.transistor_sizes.update(self.carrychainmux.generate(self.subcircuits_filename))
            self.transistor_sizes.update(self.carrychaininter.generate(self.subcircuits_filename))
            if self.specs.carry_chain_type == "skip":
                self.transistor_sizes.update(self.carrychainand.generate(self.subcircuits_filename))
                self.transistor_sizes.update(self.carrychainskipmux.generate(self.subcircuits_filename))

        # generate the carry chain of the new architecture
        if self.updates in (2, 3):
            self.transistor_sizes.update(self.carrychain2.generate(self.subcircuits_filename))
            self.transistor_sizes.update(self.carrychainperf2.generate(self.subcircuits_filename))
            self.transistor_sizes.update(self.carrychaininter2.generate(self.subcircuits_filename))

        if self.specs.enable_bram_block == 1:
            self.transistor_sizes.update(self.RAM.generate(self.subcircuits_filename, self.specs.min_tran_width, self.specs))

        for hardblock in self.hardblocklist:
            self.transistor_sizes.update(hardblock.generate(self.subcircuits_filename, self.specs.min_tran_width))
        
        # Add file footer to 'subcircuits.l' library.
        self._end_lib_files()
        
        # Create SPICE library that contains process data and voltage level information
        self._generate_process_data()
        
        # This generates an include file. Top-level SPICE netlists only need to include
        # this 'include' file to include all libraries (for convenience).
        self._generate_includes()
        
        # Create the sweep_data.l file. COFFE will use this to perform multi-variable sweeps.
        self._generate_sweep_data()
        
        # Generate top-level files. These top-level files are the files that COFFE uses to measure 
        # the delay of FPGA circuitry. 
        self.sb_mux.generate_top()
        self.cb_mux.generate_top()
        self.logic_cluster.generate_top()

        if self.specs.enable_carry_chain == 1:
            self.carrychain.generate_top()
            self.carrychainperf.generate_top()
            if not self.updates:
                self.carrychainmux.generate_top()
            self.carrychaininter.generate_top()
            if self.specs.carry_chain_type == "skip":
                self.carrychainand.generate_top()
                self.carrychainskipmux.generate_top()

        # generate the top of second carry chain
        if self.updates in (2, 3):
            self.carrychain2.generate_top()
            self.carrychainperf2.generate_top()
            self.carrychaininter2.generate_top()

        # RAM
        if self.specs.enable_bram_block == 1:
            self.RAM.generate_top()

        for hardblock in self.hardblocklist:
            hardblock.generate_top()

        # Calculate area, and wire data.
        print "Calculating area..."
        # Update area values
        self.update_area()
        print "Calculating wire lengths..."
        self.update_wires()
        print "Calculating wire resistance and capacitance..."
        self.update_wire_rc()
    
        print ""
        

    def update_area(self):
        """ This function updates self.area_dict. It passes area_dict to member objects (like sb_mux)
            to update their area. Then, with an up-to-date area_dict it, calculate total tile area. """
        
        # We use the self.transistor_sizes to compute area. This dictionary has the form 'name': 'size'
        # And it knows the transistor sizes of all transistors in the FPGA
        # We first need to calculate the area for each transistor.
        # This function stores the areas in the transistor_area_list
        self._update_area_per_transistor()
        # Now, we have to update area_dict and width_dict with the new transistor area values
        # for the basic subcircuits which are inverteres, ptran, tgate, restorers and transistors
        self._update_area_and_width_dicts()
        

        # Calculate area of SRAM
        self.area_dict["sram"] = self.specs.sram_cell_area * self.specs.min_width_tran_area
        self.area_dict["ramsram"] = 5 * self.specs.min_width_tran_area
        #MTJ in terms of min transistor width
        self.area_dict["rammtj"] = 1.23494 * self.specs.min_width_tran_area
        self.area_dict["mininv"] =  3 * self.specs.min_width_tran_area
        self.area_dict["ramtgate"] =  3 * self.area_dict["mininv"]


        # carry chain:
        if self.specs.enable_carry_chain == 1:
            self.carrychainperf.update_area(self.area_dict, self.width_dict)
            if not self.updates:
                self.carrychainmux.update_area(self.area_dict, self.width_dict)
            self.carrychaininter.update_area(self.area_dict, self.width_dict)
            self.carrychain.update_area(self.area_dict, self.width_dict)
            if self.specs.carry_chain_type == "skip":
                self.carrychainand.update_area(self.area_dict, self.width_dict)
                self.carrychainskipmux.update_area(self.area_dict, self.width_dict)

        # carry chain 2
        if self.updates in (2, 3):
            self.carrychainperf2.update_area(self.area_dict, self.width_dict)
            self.carrychaininter2.update_area(self.area_dict, self.width_dict)
            self.carrychain2.update_area(self.area_dict, self.width_dict)

        # Call area calculation functions of sub-blocks
        self.sb_mux.update_area(self.area_dict, self.width_dict)
        self.cb_mux.update_area(self.area_dict, self.width_dict)
        self.logic_cluster.update_area(self.area_dict, self.width_dict)
        

        for hardblock in self.hardblocklist:
            hardblock.update_area(self.area_dict, self.width_dict)
        
        if self.specs.enable_bram_block == 1:
            self.RAM.update_area(self.area_dict, self.width_dict)
        
        # Calculate total area of switch block
        switch_block_area = self.sb_mux.num_per_tile*self.area_dict[self.sb_mux.name + "_sram"]
        self.area_dict["sb_total"] = switch_block_area
        self.width_dict["sb_total"] = math.sqrt(switch_block_area)
        
        # Calculate total area of connection block
        connection_block_area = self.cb_mux.num_per_tile*self.area_dict[self.cb_mux.name + "_sram"]
        self.area_dict["cb_total"] = connection_block_area
        self.width_dict["cb_total"] = math.sqrt(connection_block_area)
        
        if self.lb_height == 0.0:        
            # Calculate total area of local muxes
            local_mux_area = self.logic_cluster.local_mux.num_per_tile*self.area_dict[self.logic_cluster.local_mux.name + "_sram"]
            self.area_dict["local_mux_total"] = local_mux_area
            self.width_dict["local_mux_total"] = math.sqrt(local_mux_area)
            
            #JUNIUS - calculate total area of adder direct local muxes in LUT skip (mode 10)
            if self.updates == 10:
                adder_direct_local_mux_area = self.logic_cluster.adder_direct_local_mux.num_per_tile*self.area_dict[self.logic_cluster.adder_direct_local_mux.name + "_sram"]
                self.area_dict["adder_direct_local_mux_total"] = adder_direct_local_mux_area
                self.width_dict["adder_direct_local_mux_total"] = math.sqrt(adder_direct_local_mux_area)

            # Calculate total lut area
            lut_area = self.specs.N*self.area_dict["lut_and_drivers"]
            self.area_dict["lut_total"] = lut_area
            self.width_dict["lut_total"] = math.sqrt(lut_area)

            # Calculate total ff area
            if self.updates == 4:
                ff_area = self.specs.N * (3*self.area_dict[self.logic_cluster.ble.ff2.name] +
                                            self.area_dict[self.logic_cluster.ble.ff3.name])
            elif self.updates == 3:
                ff_area = self.specs.N * (2*self.area_dict[self.logic_cluster.ble.ff.name] +
                                            self.area_dict[self.logic_cluster.ble.ff3.name] +
                                            self.area_dict[self.logic_cluster.ble.ff4.name])
            elif self.updates:
                ff_area = self.specs.N * (2*self.area_dict[self.logic_cluster.ble.ff.name] +
                                            self.area_dict[self.logic_cluster.ble.ff2.name] +
                                            self.area_dict[self.logic_cluster.ble.ff3.name])
            else:
                ff_area = self.specs.N * self.area_dict[self.logic_cluster.ble.ff.name]
                # BUG: fluts have double the number of ff
                #if self.use_fluts:
                #    ff_area *= 2

            self.area_dict["ff_total"] = ff_area
            self.width_dict["ff_total"] = math.sqrt(ff_area)
            
            # Calcualte total ble output area
            ble_output_area = self.specs.N*(self.area_dict["ble_output"])
            self.area_dict["ble_output_total"] = ble_output_area
            self.width_dict["ble_output_total"] = math.sqrt(ble_output_area)
            
            # Calculate area of logic cluster
            cluster_area = local_mux_area + self.specs.N*self.area_dict["ble"]
            if self.specs.enable_carry_chain == 1:
                cluster_area += self.area_dict[self.carrychaininter.name]

            if self.updates in (2, 3):
                cluster_area += self.area_dict[self.carrychaininter2.name]
            elif self.updates == 10:
                #JUNIUS - add adder direct local MUX to cluster area for LUT skip (mode 10)
                cluster_area += self.area_dict["adder_direct_local_mux_total"]
        else:

            # lets do it assuming a given order for the wire updates and no minimum width on sram size.
            sb_area_total = self.sb_mux.num_per_tile*self.area_dict[self.sb_mux.name]
            sb_area_sram =  self.sb_mux.num_per_tile*self.area_dict[self.sb_mux.name + "_sram"] - sb_area_total
            cb_area_total = self.cb_mux.num_per_tile*self.area_dict[self.cb_mux.name]
            cb_area_total_sram = self.cb_mux.num_per_tile*self.area_dict[self.cb_mux.name + "_sram"] - cb_area_total

            local_mux_area = self.logic_cluster.local_mux.num_per_tile*self.area_dict[self.logic_cluster.local_mux.name]            
            local_mux_sram_area = self.logic_cluster.local_mux.num_per_tile* (self.area_dict[self.logic_cluster.local_mux.name + "_sram"] - self.area_dict[self.logic_cluster.local_mux.name])

            lut_area = self.specs.N * self.area_dict["lut_and_drivers"] - self.specs.N * (2**self.specs.K) * self.area_dict["sram"]
            lut_area_sram = self.specs.N*(2**self.specs.K)*self.area_dict["sram"]

            if not self.updates:
                ffableout_area_total = self.specs.N*self.area_dict[self.logic_cluster.ble.ff.name]
                if self.specs.use_fluts:
                    ffableout_area_total = 2 * ffableout_area_total
            elif self.updates == 4:
                ffableout_area_total = self.specs.N * (3*self.area_dict[self.logic_cluster.ble.ff2.name] +
                                                         self.area_dict[self.logic_cluster.ble.ff3.name])
            elif self.updates == 3:
                ffableout_area_total = self.specs.N * (2*self.area_dict[self.logic_cluster.ble.ff.name] +
                                                         self.area_dict[self.logic_cluster.ble.ff3.name] +
                                                         self.area_dict[self.logic_cluster.ble.ff4.name])
            elif self.updates:
                ffableout_area_total = self.specs.N * (2*self.area_dict[self.logic_cluster.ble.ff.name] +
                                                         self.area_dict[self.logic_cluster.ble.ff2.name] +
                                                         self.area_dict[self.logic_cluster.ble.ff3.name])

            ffableout_area_total += self.specs.N*(self.area_dict["ble_output"])


            cc_area_total = 0.0
            skip_size = 5
            self.carry_skip_periphery_count = int(math.floor((self.specs.N * self.specs.FAs_per_flut)/skip_size))

            if not self.updates:
                if self.specs.enable_carry_chain == 1:
                    cc_area_total =  self.specs.N * self.specs.FAs_per_flut * (self.area_dict[self.carrychain.name] + self.area_dict[self.carrychainmux.name])
                    if not (self.carry_skip_periphery_count == 0 or self.specs.carry_chain_type == "ripple"):
                        cc_area_total +=  self.carry_skip_periphery_count * (self.area_dict["xcarry_chain_and"] + self.area_dict["xcarry_chain_mux"])
                    cc_area_total += self.area_dict[self.carrychaininter.name]
            else:
                cc_area_total = self.specs.N * 2 * self.area_dict[self.carrychain.name] + self.area_dict[self.carrychaininter.name]
                # if new architecture add the second carry chain
                if self.updates in (2, 3):
                    cc_area_total += self.specs.N * 2 * self.area_dict[self.carrychain2.name] + self.area_dict[self.carrychaininter2.name]
                # Add two more adders per ALM for the Stratix10 level 3 arhcitecture
                elif self.updates == 4:
                    cc_area_total += self.specs.N * 2 * self.area_dict[self.carrychain.name]


            cluster_area = local_mux_area + local_mux_sram_area + ffableout_area_total + cc_area_total + lut_area + lut_area_sram
            #JUNIUS - add adder direct local MUX to cluster area, and update adder direct local mux area for LUT skip (mode 10)
            if self.updates == 10:
                adder_direct_local_mux_area = self.logic_cluster.adder_direct_local_mux.num_per_tile*self.area_dict[self.logic_cluster.adder_direct_local_mux.name]            
                adder_direct_local_mux_sram_area = self.logic_cluster.adder_direct_local_mux.num_per_tile* (self.area_dict[self.logic_cluster.adder_direct_local_mux.name + "_sram"] - self.area_dict[self.logic_cluster.adder_direct_local_mux.name])
                cluster_area += adder_direct_local_mux_area + adder_direct_local_mux_sram_area
                self.area_dict["adder_direct_local_mux_total"] = adder_direct_local_mux_area + adder_direct_local_mux_area
                self.width_dict["adder_direct_local_mux_total"] = math.sqrt(adder_direct_local_mux_area + adder_direct_local_mux_area)

            self.area_dict["cc_area_total"] = cc_area_total
            self.width_dict["cc_area_total"] = math.sqrt(cc_area_total)

            self.area_dict["local_mux_total"] = local_mux_area + local_mux_area
            self.width_dict["local_mux_total"] = math.sqrt(local_mux_area + local_mux_area)

            self.area_dict["lut_total"] = lut_area + self.specs.N*(2**self.specs.K)*self.area_dict["sram"]
            self.width_dict["lut_total"] = math.sqrt(lut_area + self.specs.N*(2**self.specs.K)*self.area_dict["sram"])

            # Calculate total ff area
            if self.updates == 4:
                ff_area = self.specs.N * (3*self.area_dict[self.logic_cluster.ble.ff2.name] +
                                            self.area_dict[self.logic_cluster.ble.ff3.name])
            elif self.updates == 3:
                ff_area = self.specs.N * (2*self.area_dict[self.logic_cluster.ble.ff.name] +
                                            self.area_dict[self.logic_cluster.ble.ff3.name] +
                                            self.area_dict[self.logic_cluster.ble.ff4.name])
            elif self.updates:
                ff_area = self.specs.N * (2*self.area_dict[self.logic_cluster.ble.ff.name] +
                                            self.area_dict[self.logic_cluster.ble.ff2.name] +
                                            self.area_dict[self.logic_cluster.ble.ff3.name])
            else:
                ff_area = self.specs.N * self.area_dict[self.logic_cluster.ble.ff.name]

            self.area_dict["ff_total"] = ff_area
            self.width_dict["ff_total"] = math.sqrt(ff_area)

            self.area_dict["ffableout_area_total"] = ffableout_area_total
            self.width_dict["ffableout_area_total"] = math.sqrt(ffableout_area_total)            

            self.area_dict["ble_output_total"] = self.specs.N*(self.area_dict["ble_output"])
            self.width_dict["ble_output_total"] = math.sqrt(self.specs.N*(self.area_dict["ble_output"]))



        self.area_dict["logic_cluster"] = cluster_area
        self.width_dict["logic_cluster"] = math.sqrt(cluster_area)

        # TODO: clean this up
        # Calculate Carry Chain Area
        # already included in bles, extracting for the report
        if not self.updates:
            if self.specs.enable_carry_chain:
                carry_chain_area = self.specs.N * self.specs.FAs_per_flut * (self.area_dict[self.carrychain.name] + self.area_dict[self.carrychainmux.name])
                carry_chain_area +=  self.area_dict[self.carrychaininter.name]
                if self.specs.carry_chain_type == "skip":
                    self.carry_skip_periphery_count = int(math.floor((self.specs.N * self.specs.FAs_per_flut)/self.skip_size))
                    carry_chain_area += self.carry_skip_periphery_count *(self.area_dict["xcarry_chain_and"] + self.area_dict["xcarry_chain_mux"])
        else:
            carry_chain_area = self.specs.N * self.specs.FAs_per_flut * self.area_dict[self.carrychain.name] + self.area_dict[self.carrychaininter.name]
            if self.updates in (2, 3):
                carry_chain_area += self.specs.N * self.specs.FAs_per_flut * self.area_dict[self.carrychain2.name] + self.area_dict[self.carrychaininter2.name]
            # Add two more adders per ALM for the Stratix10 level 3 design
            elif self.updates == 4:
                carry_chain_area += self.specs.N * self.specs.FAs_per_flut * self.area_dict[self.carrychain.name]
            
        if self.updates or self.specs.enable_carry_chain:
            self.area_dict["total_carry_chain"] = carry_chain_area
        
        # Calculate tile area
        tile_area = switch_block_area + connection_block_area + cluster_area 

        if self.specs.mult_size != 0:
            #print("Tile area w/o multiplier: " + str(tile_area))
            # Add the hard multiplier size added to the Stratix10 architecture this value 
            # should be defined in the architecture file, if not its default value will be 0
            tile_area += self.specs.mult_size*1e6
            #print("Tile area w/ multiplier: " + str(tile_area))

        self.area_dict["tile"] = tile_area
        self.width_dict["tile"] = math.sqrt(tile_area)

        
        if self.specs.enable_bram_block == 1:
            # Calculate RAM area:

            # LOCAL MUX + FF area
            RAM_local_mux_area = self.RAM.RAM_local_mux.num_per_tile * self.area_dict[self.RAM.RAM_local_mux.name + "_sram"] + self.area_dict[self.logic_cluster.ble.ff.name]
            self.area_dict["ram_local_mux_total"] = RAM_local_mux_area
            self.width_dict["ram_local_mux_total"] = math.sqrt(RAM_local_mux_area)

            # SB and CB in the RAM tile:
            RAM_area =(RAM_local_mux_area + self.area_dict[self.cb_mux.name + "_sram"] * self.RAM.ram_inputs + (2** (self.RAM.conf_decoder_bits + 3)) *self.area_dict[self.sb_mux.name + "_sram"]) 
            RAM_SB_area = 2** (self.RAM.conf_decoder_bits + 3) *self.area_dict[self.sb_mux.name + "_sram"] 
            RAM_CB_area =  self.area_dict[self.cb_mux.name + "_sram"] * self.RAM.ram_inputs 


            self.area_dict["level_shifters"] = self.area_dict["level_shifter"] * self.RAM.RAM_local_mux.num_per_tile
            self.area_dict["RAM_SB"] = RAM_SB_area
            self.area_dict["RAM_CB"] = RAM_CB_area
            # Row decoder area calculation
 
            RAM_decoder_area = 0.0
            RAM_decoder_area += self.area_dict["rowdecoderstage0"]
            #if there is a predecoder, add its area
            if self.RAM.valid_row_dec_size3 == 1:
                RAM_decoder_area += self.area_dict["rowdecoderstage13"]
            #if there is a predecoder, add its area
            if self.RAM.valid_row_dec_size2 == 1:
                RAM_decoder_area += self.area_dict["rowdecoderstage12"]
            #if there is a predecoder, add its area
            RAM_decoder_area += self.area_dict["rowdecoderstage3"]
            # There are two decoders in a dual port circuit:
            RAM_area += RAM_decoder_area * 2 
            # add the actual array area to total RAM area
            self.area_dict["memorycell_total"] = self.area_dict["memorycell"]
            RAM_area += self.area_dict["memorycell_total"]

            if self.RAM.memory_technology == "SRAM":
            # add precharge, write driver, and sense amp area to total RAM area
                self.area_dict["precharge_total"] = (self.area_dict[self.RAM.precharge.name] * 2* (2**(self.RAM.conf_decoder_bits+self.RAM.col_decoder_bits))) * self.number_of_banks
                # several components will be doubled for the largest decoder size to prevent a large amount of delay.
                if self.RAM.row_decoder_bits == 9:
                    self.area_dict["precharge_total"] = 2 * self.area_dict["precharge_total"]
                self.area_dict["samp_total"] = self.area_dict[self.RAM.samp.name] * 2* 2**(self.RAM.conf_decoder_bits) * self.number_of_banks 
                self.area_dict["writedriver_total"] = self.area_dict[self.RAM.writedriver.name] * 2* 2**(self.RAM.conf_decoder_bits) * self.number_of_banks 
                RAM_area += (self.area_dict["precharge_total"] + self.area_dict["samp_total"] + self.area_dict["writedriver_total"])
                self.area_dict["columndecoder_total"] = ((self.area_dict["ramtgate"] * 4 *  (2**(self.RAM.conf_decoder_bits+self.RAM.col_decoder_bits))) / (2**(self.RAM.col_decoder_bits))) + self.area_dict["columndecoder"] * 2 
            
            else:
                # In case of MTJ, banks can share sense amps so we don't have mutlitplication by two
                self.area_dict["samp_total"] = self.area_dict["mtj_subcircuits_sa"] * 2**(self.RAM.conf_decoder_bits) * self.number_of_banks 
                # Write driver can't be shared:
                self.area_dict["writedriver_total"] = self.area_dict["mtj_subcircuits_writedriver"] * 2* 2**(self.RAM.conf_decoder_bits) * self.number_of_banks 
                self.area_dict["cs_total"] = self.area_dict["mtj_subcircuits_cs"] * 2* 2**(self.RAM.conf_decoder_bits +self.RAM.col_decoder_bits) * self.number_of_banks 
                if self.RAM.row_decoder_bits == 9:
                    self.area_dict["cs_total"] = 2 * self.area_dict["cs_total"]

                self.area_dict["columndecoder_total"] = self.area_dict["columndecoder"] * 2 
                RAM_area +=  self.area_dict["samp_total"] + self.area_dict["writedriver_total"] + self.area_dict["cs_total"]

            self.area_dict["columndecoder_sum"] = self.area_dict["columndecoder_total"] * self.number_of_banks 
            RAM_area += self.area_dict["columndecoder_sum"]
            #configurable decoder:
            RAM_configurabledecoder_area = self.area_dict[self.RAM.configurabledecoderi.name + "_sram"]
            if self.RAM.cvalidobj1 == 1:
                RAM_configurabledecoder_area += self.area_dict[self.RAM.configurabledecoder3ii.name]
            if self.RAM.cvalidobj2 == 1:
                RAM_configurabledecoder_area += self.area_dict[self.RAM.configurabledecoder2ii.name]
            self.area_dict["configurabledecoder_wodriver"] = RAM_configurabledecoder_area
            self.width_dict["configurabledecoder_wodriver"] = math.sqrt(self.area_dict["configurabledecoder_wodriver"])
            RAM_configurabledecoder_area += self.area_dict[self.RAM.configurabledecoderiii.name]
            if self.number_of_banks == 2:
                RAM_configurabledecoder_area = RAM_configurabledecoder_area * 2
            RAM_area += 2 * RAM_configurabledecoder_area 

            # add the output crossbar area:
            RAM_area += self.area_dict[self.RAM.pgateoutputcrossbar.name + "_sram"] 
            # add the wordline drivers:
            RAM_wordlinedriver_area = self.area_dict[self.RAM.wordlinedriver.name] * self.number_of_banks
            RAM_area += self.area_dict["level_shifters"]
            RAM_area += RAM_wordlinedriver_area

            # write into dictionaries:
            self.area_dict["wordline_total"] = RAM_wordlinedriver_area
            self.width_dict["wordline_total"] = math.sqrt(RAM_wordlinedriver_area)
            self.area_dict["configurabledecoder"] = RAM_configurabledecoder_area
            self.width_dict["configurabledecoder"] = math.sqrt(RAM_configurabledecoder_area)
            self.area_dict["decoder"] = RAM_decoder_area 
            self.area_dict["decoder_total"] = RAM_decoder_area * 2 
            self.width_dict["decoder"] = math.sqrt(RAM_decoder_area)
            self.area_dict["ram"] = RAM_area
            self.area_dict["ram_core"] = RAM_area - RAM_SB_area - RAM_CB_area
            self.width_dict["ram"] = math.sqrt(RAM_area) 
        
        if self.lb_height != 0.0:  
            self.compute_distance()



    def compute_distance(self):
        """ This function computes distances for different stripes for the floorplanner:

        """
        # todo: move these to user input
        self.stripe_order = ["sb_sram","sb","sb", "cb", "cb_sram","ic_sram", "ic","lut_sram", "lut", "cc","ffble", "lut", "lut_sram", "ic", "ic_sram", "cb_sram", "cb", "sb","sb", "sb_sram"]
        #self.stripe_order = ["cb", "cb_sram","ic_sram", "ic","lut_sram", "lut", "cc","ffble", "sb", "sb_sram"]
        self.span_stripe_fraction = 10


        self.num_cb_stripes = 0
        self.num_sb_stripes = 0
        self.num_ic_stripes = 0
        self.num_lut_stripes = 0
        self.num_ffble_stripes = 0
        self.num_cc_stripes = 0
        self.num_cbs_stripes = 0
        self.num_sbs_stripes = 0
        self.num_ics_stripes = 0
        self.num_luts_stripes = 0
        #find the number of each stripe type in the given arrangement:
        for item in self.stripe_order:
            if item == "sb":
                self.num_sb_stripes =  self.num_sb_stripes + 1
            elif item == "cb":
                self.num_cb_stripes =  self.num_cb_stripes + 1
            elif item == "ic":
                self.num_ic_stripes =  self.num_ic_stripes + 1
            elif item == "lut":
                self.num_lut_stripes =  self.num_lut_stripes + 1
            elif item == "cc":
                self.num_cc_stripes =  self.num_cc_stripes + 1
            elif item == "ffble":
                self.num_ffble_stripes =  self.num_ffble_stripes + 1
            elif item == "sb_sram":
                self.num_sbs_stripes =  self.num_sbs_stripes + 1
            elif item == "cb_sram":
                self.num_cbs_stripes =  self.num_cbs_stripes + 1
            elif item == "ic_sram":
                self.num_ics_stripes =  self.num_ics_stripes + 1
            elif item == "lut_sram":
                self.num_luts_stripes =  self.num_luts_stripes + 1

        # measure the width of each stripe:

        self.w_cb = (self.cb_mux.num_per_tile*self.area_dict[self.cb_mux.name])/(self.num_cb_stripes * self.lb_height)
        self.w_sb = (self.sb_mux.num_per_tile*self.area_dict[self.sb_mux.name])/(self.num_sb_stripes * self.lb_height)

        #JUNIUS - factor in area of adder direct local MUX in LUT skip (mode 10) into interconnect
        ic_area = self.logic_cluster.local_mux.num_per_tile*self.area_dict[self.logic_cluster.local_mux.name]
        if self.updates == 10:
            ic_area += self.logic_cluster.adder_direct_local_mux.num_per_tile*self.area_dict[self.logic_cluster.adder_direct_local_mux.name]
        self.w_ic = (ic_area)/(self.num_ic_stripes * self.lb_height)

        self.w_lut = (self.specs.N*self.area_dict["lut_and_drivers"] - self.specs.N*(2**self.specs.K)*self.area_dict["sram"])/(self.num_lut_stripes * self.lb_height)
        #if self.specs.enable_carry_chain == 1:
        self.w_cc = self.area_dict["cc_area_total"]/(self.num_cc_stripes * self.lb_height)
        self.w_ffble = self.area_dict["ffableout_area_total"]/(self.num_ffble_stripes * self.lb_height)
        self.w_scb = (self.cb_mux.num_per_tile*self.area_dict[self.cb_mux.name + "_sram"] - self.cb_mux.num_per_tile*self.area_dict[self.cb_mux.name])/(self.num_cbs_stripes * self.lb_height)
        self.w_ssb = (self.sb_mux.num_per_tile*self.area_dict[self.sb_mux.name + "_sram"] - self.sb_mux.num_per_tile*self.area_dict[self.sb_mux.name])/(self.num_sbs_stripes * self.lb_height)
        
        #JUNIUS - factor in area of adder direct local MUX in LUT skip (mode 10) into interconnect
        sic_area = self.logic_cluster.local_mux.num_per_tile * (self.area_dict[self.logic_cluster.local_mux.name + "_sram"] - self.area_dict[self.logic_cluster.local_mux.name])
        if self.updates == 10:
            sic_area += self.logic_cluster.adder_direct_local_mux.num_per_tile * (self.area_dict[self.logic_cluster.adder_direct_local_mux.name + "_sram"] - self.area_dict[self.logic_cluster.adder_direct_local_mux.name])
        self.w_sic = (sic_area)/(self.num_ics_stripes * self.lb_height)
        self.w_slut = (self.specs.N*(2**self.specs.K)*self.area_dict["sram"]) / (self.num_luts_stripes * self.lb_height)

        # create a temporary dictionary of stripe width to use in distance calculation:
        self.dict_real_widths = {}
        self.dict_real_widths["sb_sram"] = self.w_ssb
        self.dict_real_widths["sb"] = self.w_sb
        self.dict_real_widths["cb"] = self.w_cb
        self.dict_real_widths["cb_sram"] = self.w_scb
        self.dict_real_widths["ic_sram"] = self.w_sic
        self.dict_real_widths["ic"] = self.w_ic
        self.dict_real_widths["lut_sram"] = self.w_slut
        self.dict_real_widths["lut"] = self.w_lut
        #if self.specs.enable_carry_chain == 1:
        self.dict_real_widths["cc"] = self.w_cc
        self.dict_real_widths["ffble"] = self.w_ffble

        # what distances do we need?
        self.d_cb_to_ic = 0.0
        self.d_ic_to_lut = 0.0
        self.d_lut_to_cc = 0.0
        self.d_cc_to_ffble = 0.0
        self.d_ffble_to_sb = 0.0
        self.d_ffble_to_ic = 0.0

        # worst-case distance between two stripes:
        for index1, item1 in enumerate(self.stripe_order):
            for index2, item2 in enumerate(self.stripe_order):
                if item1 != item2:
                    if (item1 == "cb" and item2 == "ic") or (item1 == "ic" and item2 == "cb"):
                        if index1 < index2:
                            distance_temp = self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                            for i in range(index1 + 1, index2):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp +  self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                        else:
                            distance_temp = self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                            for i in range(index2 + 1, index1):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                        if self.d_cb_to_ic < distance_temp:
                            self.d_cb_to_ic = distance_temp

                    if (item1 == "lut" and item2 == "ic") or (item1 == "ic" and item2 == "lut"):
                        if index1 < index2:
                            distance_temp = self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                            for i in range(index1 + 1, index2):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp +  self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                        else:
                            distance_temp = self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                            for i in range(index2 + 1, index1):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                        if self.d_ic_to_lut < distance_temp:
                            self.d_ic_to_lut = distance_temp

                    if (item1 == "lut" and item2 == "cc") or (item1 == "cc" and item2 == "lut"):
                        if index1 < index2:
                            distance_temp = self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                            for i in range(index1 + 1, index2):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp +  self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                        else:
                            distance_temp = self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                            for i in range(index2 + 1, index1):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                        if self.d_lut_to_cc < distance_temp:
                            self.d_lut_to_cc = distance_temp

                    if (item1 == "ffble" and item2 == "cc") or (item1 == "cc" and item2 == "ffble"):
                        if index1 < index2:
                            distance_temp = self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                            for i in range(index1 + 1, index2):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp +  self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                        else:
                            distance_temp = self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                            for i in range(index2 + 1, index1):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                        if self.d_cc_to_ffble < distance_temp:
                            self.d_cc_to_ffble = distance_temp                                                                                    

                    if (item1 == "ffble" and item2 == "sb") or (item1 == "sb" and item2 == "ffble"):
                        if index1 < index2:
                            distance_temp = self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                            for i in range(index1 + 1, index2):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp +  self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                        else:
                            distance_temp = self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                            for i in range(index2 + 1, index1):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                        if self.d_ffble_to_sb < distance_temp:
                            self.d_ffble_to_sb = distance_temp

                    if (item1 == "ffble" and item2 == "ic") or (item1 == "ic" and item2 == "ffble"):
                        if index1 < index2:
                            distance_temp = self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                            for i in range(index1 + 1, index2):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp +  self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                        else:
                            distance_temp = self.dict_real_widths[self.stripe_order[index2]]/self.span_stripe_fraction
                            for i in range(index2 + 1, index1):
                                distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[i]]/self.span_stripe_fraction
                            distance_temp = distance_temp + self.dict_real_widths[self.stripe_order[index1]]/self.span_stripe_fraction
                        if self.d_ffble_to_ic < distance_temp:
                            self.d_ffble_to_ic = distance_temp       
        
        #print str(self.dict_real_widths["sb"])
        #print str(self.dict_real_widths["cb"])
        #print str(self.dict_real_widths["ic"])
        #print str(self.dict_real_widths["lut"])
        #print str(self.dict_real_widths["cc"])
        #print str(self.dict_real_widths["ffble"])
        #print str(self.lb_height)


    def determine_height(self):

        # if no previous floorplan exists, get an initial height:
        if self.lb_height == 0.0:
            self.lb_height = math.sqrt(self.area_dict["tile"])

        is_done = False
        current_iteration = 0
        max_iteration = 10
        # tweak the current height to find a better one, possibly:
        while is_done == False and current_iteration < max_iteration:
            print "searching for a height for the logic tile " + str(self.lb_height)
            old_height = self.lb_height
            current_best_index = 0
            self.update_area()
            self.update_wires()
            self.update_wire_rc()
            self.update_delays(self.spice_interface)
            old_cost = tran_sizing.cost_function(tran_sizing.get_eval_area(self, "global", self.sb_mux, 0, 0), tran_sizing.get_current_delay(self, 0), self.area_opt_weight, self.delay_opt_weight)
            for i in range (-10,11):
                self.lb_height = old_height + ((0.01 * (i))* old_height)
                self.update_area()
                self.update_wires()
                self.update_wire_rc()
                self.update_delays(self.spice_interface)
                new_cost = tran_sizing.cost_function(tran_sizing.get_eval_area(self, "global", self.sb_mux, 0, 0), tran_sizing.get_current_delay(self, 0), self.area_opt_weight, self.delay_opt_weight)
                if new_cost < old_cost:
                    old_cost = new_cost
                    current_best_index = i
            self.lb_height = (0.01 * (current_best_index))* old_height + old_height
            current_iteration = current_iteration + 1
            if current_best_index == 0:
                is_done = True

        print "found the best tile height: " + str(self.lb_height)

        

    def update_wires(self):
        """ This function updates self.wire_lengths and self.wire_layers. It passes wire_lengths and wire_layers to member 
            objects (like sb_mux) to update their wire lengths and layers. """
        
        # Update wire lengths and layers for all subcircuits
        if self.lb_height == 0:
            self.cluster_output_load.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, 0.0, 0.0)
            self.sb_mux.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, 1.0)
            self.cb_mux.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, 1.0)
            self.logic_cluster.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, 1.0, 1.0, 0.0, 0.0)
            self.routing_wire_load.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, 0.0, 2.0, 2.0)
        else:
            sb_ratio = (self.lb_height/(self.sb_mux.num_per_tile/self.num_sb_stripes)) / self.dict_real_widths["sb"]
            if sb_ratio < 1.0:
                sb_ratio = 1/sb_ratio
			
			#if the ratio is larger than 2.0, we can look at this stripe as two stripes put next to each other and partly fix the ratio:
				
            cb_ratio = (self.lb_height/(self.cb_mux.num_per_tile/self.num_cb_stripes)) / self.dict_real_widths["cb"]
            if cb_ratio < 1.0:
                cb_ratio = 1/cb_ratio
				
			#if the ratio is larger than 2.0, we can look at this stripe as two stripes put next to each other and partly fix the ratio:

            ic_ratio = (self.lb_height/(self.logic_cluster.local_mux.num_per_tile/self.num_ic_stripes)) / self.dict_real_widths["ic"]
            if ic_ratio < 1.0:
                ic_ratio = 1/ic_ratio
				
			#if the ratio is larger than 2.0, we can look at this stripe as two stripes put next to each other and partly fix the ratio:			

				
				
            lut_ratio = (self.lb_height/(self.specs.N/self.num_lut_stripes)) / self.dict_real_widths["lut"]
            if lut_ratio < 1.0:
                lut_ratio = 1/lut_ratio
				
			#if the ratio is larger than 2.0, we can look at this stripe as two stripes put next to each other and partly fix the ratio:
            #sb_ratio = 1.0
            #cb_ratio = 1.0
            #ic_ratio = 1.0
            #lut_ratio = 1.0

            #this was used for debugging so I commented it
            #print "ratios " + str(sb_ratio) +" "+ str(cb_ratio) +" "+ str(ic_ratio) +" "+ str(lut_ratio)
            self.cluster_output_load.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, self.d_ffble_to_sb, self.lb_height)
            self.sb_mux.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, sb_ratio)
            self.cb_mux.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, cb_ratio)
            self.logic_cluster.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, ic_ratio, lut_ratio, self.d_ffble_to_ic, self.d_cb_to_ic + self.lb_height)
            self.routing_wire_load.update_wires(self.width_dict, self.wire_lengths, self.wire_layers, self.lb_height, self.num_sb_stripes, self.num_cb_stripes)


        
        if self.specs.enable_carry_chain == 1:
            self.carrychain.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)
            self.carrychainperf.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)
            if not self.updates:
                self.carrychainmux.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)
            self.carrychaininter.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)
            if self.specs.carry_chain_type == "skip":
                self.carrychainand.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)
                self.carrychainskipmux.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)

        # updates wirs for the new architecture
        if self.updates in (2, 3):
            self.carrychain2.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)
            self.carrychainperf2.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)
            self.carrychaininter2.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)


        if self.specs.enable_bram_block == 1:
            self.RAM.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)

        for hardblock in self.hardblocklist:
            hardblock.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)  
            hardblock.mux.update_wires(self.width_dict, self.wire_lengths, self.wire_layers)     

    def update_wire_rc(self):
        """ This function updates self.wire_rc_dict based on the FPGA's self.wire_lengths and self.wire_layers."""
            
        # Calculate R and C for each wire
        for wire, length in self.wire_lengths.iteritems():
            # Get wire layer
            layer = self.wire_layers[wire]
            # Get R and C per unit length for wire layer
            rc = self.metal_stack[layer]
            # Calculate total wire R and C
            resistance = rc[0]*length
            capacitance = rc[1]*length/2
            # Add to wire_rc dictionary
            self.wire_rc_dict[wire] = (resistance, capacitance)     



    def update_delays(self, spice_interface):
        """ 
        Get the HSPICE delays for each subcircuit. 
        This function returns "False" if any of the HSPICE simulations failed.
        """
        
        print("*** UPDATING DELAYS ***")
        crit_path_delay = 0
        valid_delay = True

        # Create parameter dict of all current transistor sizes and wire rc
        parameter_dict = {}
        for tran_name, tran_size in self.transistor_sizes.iteritems():
            if not self.specs.use_finfet:
                parameter_dict[tran_name] = [1e-9*tran_size*self.specs.min_tran_width]
            else :
                parameter_dict[tran_name] = [tran_size]

        for wire_name, rc_data in self.wire_rc_dict.iteritems():
            parameter_dict[wire_name + "_res"] = [rc_data[0]]
            parameter_dict[wire_name + "_cap"] = [rc_data[1]*1e-15]

        # Run HSPICE on all subcircuits and collect the total tfall and trise for that 
        # subcircuit. We are only doing a single run on HSPICE so we expect the result
        # to be in [0] of the spice_meas dictionary. We check to make sure that the 
        # HSPICE simulation was successful by checking if any of the SPICE measurements
        # were "failed". If that is the case, we set the delay of that subcircuit to 1
        # second and set our valid_delay flag to False.


        ###########################################################################################
        ###                                                                                     ### 
        ###                       Calculate Delays For All Subcircuits                          ###
        ###                 (except for LUT, LUT drivers, Hardblocks and RAMs)                  ###
        ###                                                                                     ###
        ###########################################################################################

        # This loop calculates the delay for all the subcircuits in the subcircuits dictionary
        # adding any new subcircuit to COFFE should be added to this dictionary and the function
        # _adds_to_critical_path should be moditfied to inidicate whether this new subcircuit
        # should add to the overall critical path or not
        # TODO: Remove the adds_critical_path parameter and make it a parameter in all the 
        # resizable circuits
        for name, subcircuit in self.subcircuits.iteritems():

            print("  Updating delay for " + name)

            spice_meas = spice_interface.run(subcircuit.top_spice_path, parameter_dict) 

            trise, tfall, valid_delay = utils.get_delays(spice_meas)

            subcircuit.tfall = tfall
            subcircuit.trise = trise
            subcircuit.delay = max(tfall, trise)

            self.delay_dict[name] = subcircuit.delay
            # lut delay (SRAM to output) is not added to the crictical path
            # also the power of it is not added. Both are calculated for
            # all the input drivers seperatly
            if subcircuit != self.logic_cluster.ble.lut:
                subcircuit.power = float(spice_meas["meas_avg_power"][0])
            # checks if the delay of the circuit should be added to the overall crictical path
            if self._adds_to_critical_path(subcircuit):
                crit_path_delay += (subcircuit.delay * subcircuit.delay_weight)

       
        # Measure the delay of input to Cout and input to Sout for carry chains
        if self.specs.updates:
            print("  Updating delay for Carry Chain 1 input to Sout and Cout")
            spice_meas = spice_interface.run(self.carrychain.top_meas, parameter_dict)
            trise, tfall, valid_delay = utils.get_delays(spice_meas, "meas_in_sout")
            self.carrychain.in_sout_delay = max(tfall, trise)
            trise, tfall, valid_delay = utils.get_delays(spice_meas, "meas_in_cout")
            self.carrychain.in_cout_delay = max(tfall, trise)

            if self.specs.updates in (2, 3):
                print("  Updating delay for Carry Chain 2 input to Sout and Cout")
                spice_meas = spice_interface.run(self.carrychain2.top_meas, parameter_dict)
                trise, tfall, valid_delay = utils.get_delays(spice_meas, "meas_in_sout")
                self.carrychain2.in_sout_delay = max(tfall, trise)
                trise, tfall, valid_delay = utils.get_delays(spice_meas, "meas_in_cout")
                self.carrychain2.in_cout_delay = max(tfall, trise)


        ###########################################################################################
        ###                                                                                     ### 
        ###                     LUT Input Driver and Driver Not Delays                          ###
        ###                                                                                     ###
        ###########################################################################################

        for lut_input_name, lut_input in self.logic_cluster.ble.lut.input_drivers.iteritems():
            for driver in [lut_input.driver, lut_input.not_driver]:

                print("  Updating delay for " + driver.name) 
                spice_meas = spice_interface.run(driver.top_spice_path, parameter_dict) 
                trise, tfall, valid_delay = utils.get_delays(spice_meas)
                driver.tfall = tfall
                driver.trise = trise
                driver.delay = max(tfall, trise)
                driver.power = float(spice_meas["meas_avg_power"][0])
                self.delay_dict[driver.name] = driver.delay

                if driver.delay < 0 :
                    print("*** "+driver.name+"is negative : " + str(driver.delay) + " ***")
                    exit(2)                


        ###########################################################################################
        ###                                                                                     ### 
        ###                          LUT Delay (Input to Output)                                ###
        ###                                                                                     ###
        ###########################################################################################

        # Get delay for all paths through the LUT.
        # We get delay for each path through the LUT as well as for the LUT input drivers.
        for lut_input_name, lut_input in self.logic_cluster.ble.lut.input_drivers.iteritems():

            driver = lut_input.driver
            not_driver = lut_input.not_driver
            print("  Updating delay for " + driver.name.replace("_driver", ""))
            driver_and_lut_sp_path = driver.top_spice_path.replace(".sp", "_with_lut.sp")

            # If this is a fracturable input, input controlling one of the fracturable muxes
            # then its delay is the delay of the signal propagation in the mux.
            # TODO: the fmux delay is the delay of propagating through the mux and not the 
            # delay of switching the mux how is this used as the input delay. I think it 
            # should calculated as the new design where the delay is calculated from the 
            # change in the input of the fmux gate to its output.
            if (not self.updates and 
               ((lut_input_name == "f" and self.specs.use_fluts and self.specs.K == 6) or 
                (lut_input_name == "e" and self.specs.use_fluts and self.specs.K == 5))): 

                lut_input.tfall = self.logic_cluster.ble.fmux.tfall
                lut_input.trise = self.logic_cluster.ble.fmux.trise
                lut_input.delay = max(lut_input.tfall, lut_input.trise)
                lut_input.power = self.logic_cluster.ble.fmux.power

            else:
                # Get the delay for a path through the LUT (we do it for each input)
                spice_meas = spice_interface.run(driver_and_lut_sp_path, parameter_dict) 
                trise, tfall, valid_delay = utils.get_delays(spice_meas)

                # for lut c in design 4 we need to measure the delay of the input select mux after the c driver
                if self.updates == 4 and lut_input_name == 'c':
                    mspice_meas = spice_interface.run(lut_input.input_select_mux.top_spice_path, parameter_dict) 
                    mtrise, mtfall, mvalid_delay = utils.get_delays(mspice_meas)
                    lut_input.input_select_mux.tfall = mtfall
                    lut_input.input_select_mux.trise = mtrise
                    lut_input.input_select_mux.delay = max(mtfall, mtrise)
                    lut_input.input_select_mux.power = float(mspice_meas["meas_avg_power"][0])
                    self.delay_dict[lut_input.input_select_mux.name] = lut_input.input_select_mux.delay
                
                # For the Stratix10 design add the delay for thes signal propagation through fmux_l1 and
                # fmux_l2 for inputs a to d, since those inputs don't control the muxes select signals
                # input f delay will be only the delay of switching the ptran of the level 2 fmux
                # TODO: add to the delay dictionary the delay of a to d without the fmux delays might be useful
                if self.updates in (1, 2, 3, 10): 
                    if lut_input_name != 'e' and lut_input_name != 'f' :
                        tfall += self.logic_cluster.ble.fmux.tfall + self.logic_cluster.ble.fmux_l2.tfall
                        trise += self.logic_cluster.ble.fmux.trise + self.logic_cluster.ble.fmux_l2.trise
                    # input e will see the delay of switching the gates of the fmux_l1 in addition to the
                    # propagation delay through fmux_l2
                    elif lut_input_name == 'e':
                        tfall += self.logic_cluster.ble.fmux_l2.tfall
                        trise += self.logic_cluster.ble.fmux_l2.trise
                elif self.updates == 4:
                    if lut_input_name not in ('d', 'e', 'f'):
                        tfall += self.logic_cluster.ble.fmux.tfall + self.logic_cluster.ble.fmux_l2.tfall + self.logic_cluster.ble.fmux_l3.tfall
                        trise += self.logic_cluster.ble.fmux.trise + self.logic_cluster.ble.fmux_l2.trise + self.logic_cluster.ble.fmux_l3.trise
                    # for input d we need to add the delay of the two following fmuxes
                    elif lut_input_name == 'd':
                        tfall += self.logic_cluster.ble.fmux_l2.tfall + self.logic_cluster.ble.fmux_l3.tfall
                        trise += self.logic_cluster.ble.fmux_l2.trise + self.logic_cluster.ble.fmux_l3.trise
                    # for input e we need to add the delay of the last fmux
                    elif lut_input_name == 'e':
                        tfall += self.logic_cluster.ble.fmux_l3.tfall
                        trise += self.logic_cluster.ble.fmux_l3.trise
                elif self.specs.use_fluts:
                    tfall += self.logic_cluster.ble.fmux.tfall
                    trise += self.logic_cluster.ble.fmux.trise

                # add the input mux delay
                if self.updates == 4 and lut_input_name == 'c':
                    tfall += lut_input.input_select_mux.tfall
                    trise += lut_input.input_select_mux.trise

                lut_input.tfall = tfall
                lut_input.trise = trise
                lut_input.delay = max(tfall, trise)
                lut_input.power = float(spice_meas["meas_avg_power"][0])


            if lut_input.delay < 0 :
                print "*** Lut input delay is negative : " + str(lut_input.delay) + "  in path: " + driver_and_lut_sp_path +  "***"
                exit(2)

            self.delay_dict[lut_input.name] = lut_input.delay
            
            lut_delay = lut_input.delay + max(driver.delay, not_driver.delay)

            if not self.updates:
                # TODO: remove this it's a repetition, lut_delay already has 
                # the fmux delay added to it!
                if self.specs.use_fluts:
                    lut_delay += self.logic_cluster.ble.fmux.delay

            if lut_delay < 0 :
                print("*** Lut delay is negative : " + str(lut_delay) + " ***")
                exit(2)

            crit_path_delay += lut_delay*lut_input.delay_weight


        # TODO: the actual delay weights should be calculated from vpr and used here
        if self.updates:
            crit_path_delay += (self.logic_cluster.ble.fmux.delay + self.logic_cluster.ble.fmux_l2.delay) * DELAY_WEIGHT_LUT_FRAC
        elif self.specs.use_fluts:
            crit_path_delay += self.logic_cluster.ble.fmux.delay * DELAY_WEIGHT_LUT_FRAC

        self.delay_dict["rep_crit_path"] = crit_path_delay  
        

        ###########################################################################################
        ###                                                                                     ### 
        ###                                Hardblock Delays                                     ###
        ###                                                                                     ###
        ###########################################################################################

        for hardblock in self.hardblocklist:

            spice_meas = spice_interface.run(hardblock.mux.top_spice_path, 
                                             parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            hardblock.mux.tfall = tfall
            hardblock.mux.trise = trise
            hardblock.mux.delay = max(tfall, trise)

            self.delay_dict[hardblock.mux.name] = hardblock.mux.delay
            hardblock.mux.power = float(spice_meas["meas_avg_power"][0])            

            if hardblock.parameters['num_dedicated_outputs'] > 0:
				spice_meas = spice_interface.run(hardblock.dedicated.top_spice_path, 
												parameter_dict) 
				if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
					valid_delay = False
					tfall = 1
					trise = 1
				else :  
					tfall = float(spice_meas["meas_total_tfall"][0])
					trise = float(spice_meas["meas_total_trise"][0])
				if tfall < 0 or trise < 0 :
					valid_delay = False
				hardblock.dedicated.tfall = tfall
				hardblock.dedicated.trise = trise
				hardblock.dedicated.delay = max(tfall, trise)

				self.delay_dict[hardblock.dedicated.name] = hardblock.dedicated.delay
				hardblock.dedicated.power = float(spice_meas["meas_avg_power"][0])      


        # If there is no need for memory simulation, end here.
        if self.specs.enable_bram_block == 0:
            return valid_delay


        ###########################################################################################
        ###                                                                                     ### 
        ###                                RAM Block Delays                                     ###
        ###                                                                                     ###
        ###########################################################################################

        # Local RAM MUX
        print "  Updating delay for " + self.RAM.RAM_local_mux.name
        spice_meas = spice_interface.run(self.RAM.RAM_local_mux.top_spice_path, 
                                         parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.RAM_local_mux.tfall = tfall
        self.RAM.RAM_local_mux.trise = trise
        self.RAM.RAM_local_mux.delay = max(tfall, trise)
        self.delay_dict[self.RAM.RAM_local_mux.name] = self.RAM.RAM_local_mux.delay
        self.RAM.RAM_local_mux.power = float(spice_meas["meas_avg_power"][0])

        #RAM decoder units
        print "  Updating delay for " + self.RAM.rowdecoder_stage0.name
        spice_meas = spice_interface.run(self.RAM.rowdecoder_stage0.top_spice_path, parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.rowdecoder_stage0.tfall = tfall
        self.RAM.rowdecoder_stage0.trise = trise
        self.RAM.rowdecoder_stage0.delay = max(tfall, trise)
        #crit_path_delay += (self.RAM.rowdecoder_stage0.delay* self.RAM.delay_weight)
        self.delay_dict[self.RAM.rowdecoder_stage0.name] = self.RAM.rowdecoder_stage0.delay
        self.RAM.rowdecoder_stage0.power = float(spice_meas["meas_avg_power"][0])


        if self.RAM.valid_row_dec_size2 == 1:
            print "  Updating delay for " + self.RAM.rowdecoder_stage1_size2.name
            spice_meas = spice_interface.run(self.RAM.rowdecoder_stage1_size2.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.rowdecoder_stage1_size2.tfall = tfall
            self.RAM.rowdecoder_stage1_size2.trise = trise
            self.RAM.rowdecoder_stage1_size2.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.rowdecoder_stage1_size2.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.rowdecoder_stage1_size2.name] = self.RAM.rowdecoder_stage1_size2.delay
            self.RAM.rowdecoder_stage1_size2.power = float(spice_meas["meas_avg_power"][0])

        if self.RAM.valid_row_dec_size3 == 1:
            print "  Updating delay for " + self.RAM.rowdecoder_stage1_size3.name
            spice_meas = spice_interface.run(self.RAM.rowdecoder_stage1_size3.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.rowdecoder_stage1_size3.tfall = tfall
            self.RAM.rowdecoder_stage1_size3.trise = trise
            self.RAM.rowdecoder_stage1_size3.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.rowdecoder_stage1_size3.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.rowdecoder_stage1_size3.name] = self.RAM.rowdecoder_stage1_size3.delay
            self.RAM.rowdecoder_stage1_size3.power = float(spice_meas["meas_avg_power"][0])


        print "  Updating delay for " + self.RAM.rowdecoder_stage3.name
        spice_meas = spice_interface.run(self.RAM.rowdecoder_stage3.top_spice_path, parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.rowdecoder_stage3.tfall = tfall
        self.RAM.rowdecoder_stage3.trise = trise
        self.RAM.rowdecoder_stage3.delay = max(tfall, trise)
        self.delay_dict[self.RAM.rowdecoder_stage3.name] = self.RAM.rowdecoder_stage3.delay
        self.RAM.rowdecoder_stage3.power = float(spice_meas["meas_avg_power"][0])


        if self.RAM.memory_technology == "SRAM":
            print "  Updating delay for " + self.RAM.precharge.name
            spice_meas = spice_interface.run(self.RAM.precharge.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.precharge.tfall = tfall
            self.RAM.precharge.trise = trise
            self.RAM.precharge.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.precharge.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.precharge.name] = self.RAM.precharge.delay
            self.RAM.precharge.power = float(spice_meas["meas_avg_power"][0])

            print "  Updating delay for " + self.RAM.samp_part2.name
            spice_meas = spice_interface.run(self.RAM.samp_part2.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.samp_part2.tfall = tfall
            self.RAM.samp_part2.trise = trise 
            self.RAM.samp_part2.delay = max(tfall, trise)

            self.delay_dict[self.RAM.samp_part2.name] = self.RAM.samp_part2.delay
            self.RAM.samp_part2.power = float(spice_meas["meas_avg_power"][0])

            print "  Updating delay for " + self.RAM.samp.name
            spice_meas = spice_interface.run(self.RAM.samp.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])

            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.samp.tfall = tfall + self.RAM.samp_part2.tfall
            self.RAM.samp.trise = trise + self.RAM.samp_part2.trise

            self.RAM.samp.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.samp.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.samp.name] = self.RAM.samp.delay
            self.RAM.samp.power = float(spice_meas["meas_avg_power"][0])

            print "  Updating delay for " + self.RAM.writedriver.name
            spice_meas = spice_interface.run(self.RAM.writedriver.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.writedriver.tfall = tfall
            self.RAM.writedriver.trise = trise
            self.RAM.writedriver.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.writedriver.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.writedriver.name] = self.RAM.writedriver.delay
            self.RAM.writedriver.power = float(spice_meas["meas_avg_power"][0])

        else:
            print "  Updating delay for " + self.RAM.bldischarging.name
            spice_meas = spice_interface.run(self.RAM.bldischarging.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.bldischarging.tfall = tfall
            self.RAM.bldischarging.trise = trise
            self.RAM.bldischarging.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.bldischarging.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.bldischarging.name] = self.RAM.bldischarging.delay
            self.RAM.bldischarging.power = float(spice_meas["meas_avg_power"][0])

            print "  Updating delay for " + self.RAM.blcharging.name
            spice_meas = spice_interface.run(self.RAM.blcharging.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.blcharging.tfall = tfall
            self.RAM.blcharging.trise = trise
            self.RAM.blcharging.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.blcharging.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.blcharging.name] = self.RAM.blcharging.delay
            self.RAM.blcharging.power = float(spice_meas["meas_avg_power"][0])

            self.RAM.target_bl = 0.99* float(spice_meas["meas_outputtarget"][0])

            self.RAM._update_process_data()

            print "  Updating delay for " + self.RAM.blcharging.name
            spice_meas = spice_interface.run(self.RAM.blcharging.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.blcharging.tfall = tfall
            self.RAM.blcharging.trise = trise
            self.RAM.blcharging.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.blcharging.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.blcharging.name] = self.RAM.blcharging.delay
            self.RAM.blcharging.power = float(spice_meas["meas_avg_power"][0])
            self.RAM.target_bl = 0.99*float(spice_meas["meas_outputtarget"][0])

            self.RAM._update_process_data()

            print "  Updating delay for " + self.RAM.mtjsamp.name
            spice_meas = spice_interface.run(self.RAM.mtjsamp.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.mtjsamp.tfall = tfall
            self.RAM.mtjsamp.delay = tfall
            self.RAM.mtjsamp.trise = max(tfall, trise)
            #crit_path_delay += (self.RAM.mtjsamp.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.mtjsamp.name] = self.RAM.mtjsamp.delay
            self.RAM.mtjsamp.power = float(spice_meas["meas_avg_power"][0])

    
        print "  Updating delay for " + self.RAM.columndecoder.name
        spice_meas = spice_interface.run(self.RAM.columndecoder.top_spice_path, parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.columndecoder.tfall = tfall
        self.RAM.columndecoder.trise = trise
        self.RAM.columndecoder.delay = max(tfall, trise)
        #crit_path_delay += (self.RAM.columndecoder.delay* self.RAM.delay_weight)
        self.delay_dict[self.RAM.columndecoder.name] = self.RAM.columndecoder.delay
        self.RAM.columndecoder.power = float(spice_meas["meas_avg_power"][0])


        print "  Updating delay for " + self.RAM.configurabledecoderi.name
        spice_meas = spice_interface.run(self.RAM.configurabledecoderi.top_spice_path, parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.configurabledecoderi.tfall = tfall
        self.RAM.configurabledecoderi.trise = trise
        self.RAM.configurabledecoderi.delay = max(tfall, trise)
        #crit_path_delay += (self.RAM.configurabledecoderi.delay* self.RAM.delay_weight)
        self.delay_dict[self.RAM.configurabledecoderi.name] = self.RAM.configurabledecoderi.delay
        self.RAM.configurabledecoderi.power = float(spice_meas["meas_avg_power"][0])


        if self.RAM.cvalidobj1 ==1:
            print "  Updating delay for " + self.RAM.configurabledecoder3ii.name
            spice_meas = spice_interface.run(self.RAM.configurabledecoder3ii.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.configurabledecoder3ii.tfall = tfall
            self.RAM.configurabledecoder3ii.trise = trise
            self.RAM.configurabledecoder3ii.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.configurabledecoder3ii.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.configurabledecoder3ii.name] = self.RAM.configurabledecoder3ii.delay
            self.RAM.configurabledecoder3ii.power = float(spice_meas["meas_avg_power"][0])


        if self.RAM.cvalidobj2 ==1:
            print "  Updating delay for " + self.RAM.configurabledecoder2ii.name
            spice_meas = spice_interface.run(self.RAM.configurabledecoder2ii.top_spice_path, parameter_dict) 
            if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
                valid_delay = False
                tfall = 1
                trise = 1
            else :  
                tfall = float(spice_meas["meas_total_tfall"][0])
                trise = float(spice_meas["meas_total_trise"][0])
            if tfall < 0 or trise < 0 :
                valid_delay = False
            self.RAM.configurabledecoder2ii.tfall = tfall
            self.RAM.configurabledecoder2ii.trise = trise
            self.RAM.configurabledecoder2ii.delay = max(tfall, trise)
            #crit_path_delay += (self.RAM.configurabledecoder2ii.delay* self.RAM.delay_weight)
            self.delay_dict[self.RAM.configurabledecoder2ii.name] = self.RAM.configurabledecoder2ii.delay
            self.RAM.configurabledecoder2ii.power = float(spice_meas["meas_avg_power"][0])

        print "  Updating delay for " + self.RAM.configurabledecoderiii.name
        spice_meas = spice_interface.run(self.RAM.configurabledecoderiii.top_spice_path, parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.configurabledecoderiii.tfall = tfall
        self.RAM.configurabledecoderiii.trise = trise
        self.RAM.configurabledecoderiii.delay = max(tfall, trise)
        #crit_path_delay += (self.RAM.configurabledecoderiii.delay* self.RAM.delay_weight)
        self.delay_dict[self.RAM.configurabledecoderiii.name] = self.RAM.configurabledecoderiii.delay
        self.RAM.configurabledecoderiii.power = float(spice_meas["meas_avg_power"][0])
  

        print "  Updating delay for " + self.RAM.pgateoutputcrossbar.name
        spice_meas = spice_interface.run(self.RAM.pgateoutputcrossbar.top_spice_path, parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.pgateoutputcrossbar.tfall = tfall
        self.RAM.pgateoutputcrossbar.trise = trise
        self.RAM.pgateoutputcrossbar.delay = max(tfall, trise)
        #crit_path_delay += (self.RAM.pgateoutputcrossbar.delay* self.RAM.delay_weight)
        self.delay_dict[self.RAM.pgateoutputcrossbar.name] = self.RAM.pgateoutputcrossbar.delay
        self.RAM.pgateoutputcrossbar.power = float(spice_meas["meas_avg_power"][0])
        self.delay_dict["rep_crit_path"] = crit_path_delay    

        print "  Updating delay for " + self.RAM.wordlinedriver.name
        spice_meas = spice_interface.run(self.RAM.wordlinedriver.top_spice_path, parameter_dict) 
        if spice_meas["meas_total_tfall"][0] == "failed" or spice_meas["meas_total_trise"][0] == "failed" :
            valid_delay = False
            tfall = 1
            trise = 1
        else :  
            tfall = float(spice_meas["meas_total_tfall"][0])
            trise = float(spice_meas["meas_total_trise"][0])
        if tfall < 0 or trise < 0 :
            valid_delay = False
        self.RAM.wordlinedriver.tfall = tfall
        self.RAM.wordlinedriver.trise = trise
        self.RAM.wordlinedriver.delay = max(tfall, trise)
        #crit_path_delay += (self.RAM.wordlinedriver.delay* self.RAM.delay_weight)
        self.delay_dict[self.RAM.wordlinedriver.name] = self.RAM.wordlinedriver.delay
        self.RAM.wordlinedriver.power = float(spice_meas["meas_avg_power"][0])
        if self.RAM.wordlinedriver.wl_repeater == 1:
            self.RAM.wordlinedriver.power *=2

        return valid_delay


                  
    def update_power(self, spice_interface):
        """This funciton measures RAM core power once sizing has finished.
        It also sums up power consumed by the peripheral circuitry and converts it to energy per bit"""
        # Several timing parameters need to be updated before power can be measured accurately
        # The following will compute and store the current values for these delays
        # Create parameter dict of all current transistor sizes and wire rc

        parameter_dict = {}
        for tran_name, tran_size in self.transistor_sizes.iteritems():
            if not self.specs.use_finfet:
                parameter_dict[tran_name] = [1e-9*tran_size*self.specs.min_tran_width]
            else :
                parameter_dict[tran_name] = [tran_size]

        for wire_name, rc_data in self.wire_rc_dict.iteritems():
            parameter_dict[wire_name + "_res"] = [rc_data[0]]
            parameter_dict[wire_name + "_cap"] = [rc_data[1]*1e-15]

        # Update the file
        ram_decoder_stage1_delay = 0
        if self.RAM.valid_row_dec_size2 == 1:
            ram_decoder_stage1_delay = max(ram_decoder_stage1_delay, self.RAM.rowdecoder_stage1_size2.delay)
        if self.RAM.valid_row_dec_size3 == 1:
            ram_decoder_stage1_delay = max(self.RAM.rowdecoder_stage1_size3.delay, ram_decoder_stage1_delay)
        self.RAM.estimated_rowdecoder_delay = ram_decoder_stage1_delay    
        self.RAM.estimated_rowdecoder_delay += self.RAM.rowdecoder_stage3.delay
        ram_decoder_stage0_delay = self.RAM.rowdecoder_stage0.delay
        self.RAM.estimated_rowdecoder_delay += ram_decoder_stage0_delay

        # Measure the configurable decoder delay:
        configurable_decoder_delay = 0.0
        if self.RAM.cvalidobj1 == 1:
            configurable_decoder_delay = max(self.RAM.configurabledecoder3ii.delay, configurable_decoder_delay)
        if self.RAM.cvalidobj2 == 1:
            configurable_decoder_delay = max(self.RAM.configurabledecoder2ii.delay, configurable_decoder_delay)
        configurable_decoder_delay += self.RAM.configurabledecoderi.delay
        # This is the driving part of the configurable decoder.
        configurable_decoder_drive = self.RAM.configurabledecoderiii.delay

        if self.RAM.memory_technology == "SRAM":
            self.RAM.T1 = max(self.RAM.estimated_rowdecoder_delay, configurable_decoder_delay, self.RAM.precharge.delay)
            self.RAM.T2 = self.RAM.wordlinedriver.delay + self.RAM.samp.delay + self.RAM.samp_part2.delay  
            self.RAM.frequency = max(self.RAM.T1 + self.RAM.T2 , configurable_decoder_delay + configurable_decoder_drive)
            self.RAM.frequency += self.RAM.pgateoutputcrossbar.delay + 2e-11

        elif self.RAM.memory_technology == "MTJ":

            self.RAM.T1 = max(self.RAM.estimated_rowdecoder_delay, configurable_decoder_delay, self.RAM.bldischarging.delay)
            self.RAM.T2 = self.RAM.T1 +  max(self.RAM.wordlinedriver.delay , configurable_decoder_drive) + self.RAM.blcharging.delay
            self.RAM.T3 = self.RAM.T2 + self.RAM.mtjsamp.delay
            self.RAM.frequency = self.RAM.T2 - self.RAM.blcharging.delay + 3e-9

        self.RAM._update_process_data()

        if self.RAM.memory_technology == "SRAM":
            print "Measuring SRAM power " + self.RAM.power_sram_read.name
            spice_meas = spice_interface.run(self.RAM.power_sram_read.top_spice_path, parameter_dict) 
            self.RAM.power_sram_read.power_selected = float(spice_meas["meas_avg_power_selected"][0])
            self.RAM.power_sram_read.power_unselected = float(spice_meas["meas_avg_power_unselected"][0])

            spice_meas = spice_interface.run(self.RAM.power_sram_writelh.top_spice_path, parameter_dict) 
            self.RAM.power_sram_writelh.power_selected_writelh = float(spice_meas["meas_avg_power_selected"][0])

            spice_meas = spice_interface.run(self.RAM.power_sram_writehh.top_spice_path, parameter_dict) 
            self.RAM.power_sram_writehh.power_selected_writehh = float(spice_meas["meas_avg_power_selected"][0])

            spice_meas = spice_interface.run(self.RAM.power_sram_writep.top_spice_path, parameter_dict) 
            self.RAM.power_sram_writep.power_selected_writep = float(spice_meas["meas_avg_power_selected"][0])

            # can be used to help with debugging:
            #print "T1: " +str(self.RAM.T1)
            #print "T2: " + str(self.RAM.T2)
            #print "freq " + str(self.RAM.frequency)
            #print "selected " + str(self.RAM.power_sram_read.power_selected)
            #print "unselected " + str(self.RAM.power_sram_read.power_unselected)

            #print "selected_writelh " + str(self.RAM.power_sram_writelh.power_selected_writelh)
            #print "selected_writehh " + str(self.RAM.power_sram_writehh.power_selected_writehh)
            #print "selected_writep " + str(self.RAM.power_sram_writep.power_selected_writep)

            #print "power per bit read SRAM: " + str(self.RAM.power_sram_read.power_selected + self.RAM.power_sram_read.power_unselected)
            #print "Energy " + str((self.RAM.power_sram_read.power_selected + self.RAM.power_sram_read.power_unselected) * self.RAM.frequency)
            #print "Energy Writelh " + str(self.RAM.power_sram_writelh.power_selected_writelh * self.RAM.frequency)
            #print "Energy Writehh " + str(self.RAM.power_sram_writehh.power_selected_writehh * self.RAM.frequency)
            print "Energy Writep " + str(self.RAM.power_sram_writep.power_selected_writep * self.RAM.frequency)

            read_energy = (self.RAM.power_sram_read.power_selected + self.RAM.power_sram_read.power_unselected) * self.RAM.frequency
            write_energy = ((self.RAM.power_sram_writelh.power_selected_writelh + self.RAM.power_sram_writehh.power_selected_writehh)/2 + self.RAM.power_sram_read.power_unselected) * self.RAM.frequency

            self.RAM.core_energy = (self.RAM.read_to_write_ratio * read_energy + write_energy) /(1 + self.RAM.read_to_write_ratio)

        else:
            print "Measuring MTJ power "
            spice_meas = spice_interface.run(self.RAM.power_mtj_write.top_spice_path, parameter_dict) 
            self.RAM.power_mtj_write.powerpl = float(spice_meas["meas_avg_power_selected"][0])
            self.RAM.power_mtj_write.powernl = float(spice_meas["meas_avg_power_selectedn"][0])
            self.RAM.power_mtj_write.powerph = float(spice_meas["meas_avg_power_selectedh"][0])
            self.RAM.power_mtj_write.powernh = float(spice_meas["meas_avg_power_selectedhn"][0])

            # can be used to help with debugging:
            #print "Energy Negative Low " + str(self.RAM.power_mtj_write.powernl * self.RAM.frequency)
            #print "Energy Positive Low " + str(self.RAM.power_mtj_write.powerpl * self.RAM.frequency)
            #print "Energy Negative High " + str(self.RAM.power_mtj_write.powernh * self.RAM.frequency)
            #print "Energy Positive High " + str(self.RAM.power_mtj_write.powerph * self.RAM.frequency)
            #print "Energy " + str(((self.RAM.power_mtj_write.powerph - self.RAM.power_mtj_write.powernh + self.RAM.power_mtj_write.powerpl - self.RAM.power_mtj_write.powernl) * self.RAM.frequency)/4)

            spice_meas = spice_interface.run(self.RAM.power_mtj_read.top_spice_path, parameter_dict) 
            self.RAM.power_mtj_read.powerl = float(spice_meas["meas_avg_power_readl"][0])
            self.RAM.power_mtj_read.powerh = float(spice_meas["meas_avg_power_readh"][0])

            # can be used to help with debugging:
            #print "Energy Low Read " + str(self.RAM.power_mtj_read.powerl * self.RAM.frequency)
            #print "Energy High Read " + str(self.RAM.power_mtj_read.powerh * self.RAM.frequency)
            #print "Energy Read " + str(((self.RAM.power_mtj_read.powerl + self.RAM.power_mtj_read.powerh) * self.RAM.frequency))

            read_energy = ((self.RAM.power_mtj_read.powerl + self.RAM.power_mtj_read.powerh) * self.RAM.frequency)
            write_energy = ((self.RAM.power_mtj_write.powerph - self.RAM.power_mtj_write.powernh + self.RAM.power_mtj_write.powerpl - self.RAM.power_mtj_write.powernl) * self.RAM.frequency)/4
            self.RAM.core_energy = (self.RAM.read_to_write_ratio * read_energy + write_energy) /(1 + self.RAM.read_to_write_ratio)


        # Peripherals are not technology-specific
        # Different components powers are multiplied by the number of active components for each toggle:
        peripheral_energy = self.RAM.row_decoder_bits / 2 * self.RAM.rowdecoder_stage0.power * self.RAM.number_of_banks
        if self.RAM.valid_row_dec_size2 == 1 and self.RAM.valid_row_dec_size3 == 1:
            peripheral_energy += (self.RAM.rowdecoder_stage1_size3.power + self.RAM.rowdecoder_stage1_size2.power)/2
        elif self.RAM.valid_row_dec_size3 == 1:
            peripheral_energy += self.RAM.rowdecoder_stage1_size3.power
        else:
            peripheral_energy += self.RAM.rowdecoder_stage1_size2.power

        peripheral_energy += self.RAM.wordlinedriver.power + self.RAM.columndecoder.power

        peripheral_energy += self.RAM.configurabledecoderi.power * self.RAM.conf_decoder_bits / 2 * self.RAM.number_of_banks
        peripheral_energy += self.RAM.configurabledecoderiii.power * (1 + 2**self.RAM.conf_decoder_bits)/2

        # Convert to energy
        peripheral_energy = peripheral_energy * self.RAM.frequency

        # Add read-specific components
        self.RAM.peripheral_energy_read = peripheral_energy + self.RAM.pgateoutputcrossbar.power * (1 + 2**self.RAM.conf_decoder_bits)/2 * self.RAM.frequency
        # We need energy PER BIT. Hence:
        self.RAM.peripheral_energy_read /= 2** self.RAM.conf_decoder_bits
        # Add write-specific components (input FF to WD)
        self.RAM.peripheral_energy_write = peripheral_energy + (2** self.RAM.conf_decoder_bits * self.RAM.configurabledecoderiii.power /2) * self.RAM.frequency
        # Add write-specific components (Write enable wires)
        self.RAM.peripheral_energy_write += ((1 + 2** self.RAM.conf_decoder_bits) * self.RAM.configurabledecoderiii.power) * self.RAM.frequency
        # We want energy per bit per OP:
        self.RAM.peripheral_energy_write /= 2** self.RAM.conf_decoder_bits

        print "Core read and write energy: " +str(read_energy) + " and " +str(write_energy)
        print "Core energy per bit: " + str(self.RAM.core_energy)
        print "Peripheral energy per bit: " + str((self.RAM.peripheral_energy_read * self.RAM.read_to_write_ratio + self.RAM.peripheral_energy_write)/ (1 + self.RAM.read_to_write_ratio))

    def print_specs(self):

        print "|------------------------------------------------------------------------------|"
        print "|   FPGA Architecture Specs                                                    |"
        print "|------------------------------------------------------------------------------|"
        print ""
        print "  Number of BLEs per cluster (N): " + str(self.specs.N)
        print "  LUT size (K): " + str(self.specs.K)
        print "  Channel width (W): " + str(self.specs.W)
        print "  Wire segment length (L): " + str(self.specs.L)
        print "  Number cluster inputs (I): " + str(self.specs.I)
        print "  Number of BLE outputs to general routing: " + str(self.specs.num_ble_general_outputs)
        print "  Number of BLE outputs to local routing: " + str(self.specs.num_ble_local_outputs)
        print "  Number of cluster outputs: " + str(self.specs.num_cluster_outputs)
        print "  Switch block flexibility (Fs): " + str(self.specs.Fs)
        print "  Cluster input flexibility (Fcin): " + str(self.specs.Fcin)
        print "  Cluster output flexibility (Fcout): " + str(self.specs.Fcout)
        print "  Local MUX population (Fclocal): " + str(self.specs.Fclocal)
        print ""
        print "|------------------------------------------------------------------------------|"
        print ""
        
        
    def print_details(self, report_file):

        utils.print_and_write(report_file, "|------------------------------------------------------------------------------|")
        utils.print_and_write(report_file, "|   FPGA Implementation Details                                                |")
        utils.print_and_write(report_file, "|------------------------------------------------------------------------------|")
        utils.print_and_write(report_file, "")

        self.sb_mux.print_details(report_file)
        self.cb_mux.print_details(report_file)
        self.logic_cluster.print_details(report_file)
        self.cluster_output_load.print_details(report_file)
        self.routing_wire_load.print_details(report_file)
        if self.specs.enable_bram_block == 1:
            self.RAM.print_details(report_file)

        utils.print_and_write(report_file, "|------------------------------------------------------------------------------|")
        utils.print_and_write(report_file, "")

        return
    
    
    def _area_model(self, tran_name, tran_size):
        """ Transistor area model. 'tran_size' is the transistor drive strength in min. width transistor drive strengths. 
            Transistor area is calculated bsed on 'tran_size' and transistor type, which is determined by tags in 'tran_name'.
            Return valus is the transistor area in minimum width transistor areas. """
    
        # If inverter or transmission gate, use larger area to account for N-well spacing
        # If pass-transistor, use regular area because they don't need N-wells.
        if "inv_" in tran_name or "tgate_" in tran_name:
            if not self.specs.use_finfet :
                area = 0.518 + 0.127*tran_size + 0.428*math.sqrt(tran_size)
            else :
                area = 0.034 + 0.414*tran_size + 0.735*math.sqrt(tran_size)

        else:
            if not self.specs.use_finfet :
                area = 0.447 + 0.128*tran_size + 0.391*math.sqrt(tran_size)
            else :
                area = -0.013 + 0.414*tran_size + 0.665*math.sqrt(tran_size)
    
        return area    
    
     
    def _create_lib_files(self):
        """ Create SPICE library files and add headers. """

        # Create Subcircuits file
        sc_file = open(self.subcircuits_filename, 'w')
        sc_file.write("*** SUBCIRCUITS\n\n")
        sc_file.write(".LIB SUBCIRCUITS\n\n")
        sc_file.close()
       

    def _end_lib_files(self):
        """ End the SPICE library files. """

        # Subcircuits file
        sc_file = open(self.subcircuits_filename, 'a')
        sc_file.write(".ENDL SUBCIRCUITS")
        sc_file.close()
       

    def _generate_basic_subcircuits(self):
        """ Generates the basic subcircuits SPICE file (pass-transistor, inverter, etc.) """
        
        print "Generating basic subcircuits"
        
        # Open basic subcircuits file and write heading
        basic_sc_file = open(self.basic_subcircuits_filename, 'w')
        basic_sc_file.write("*** BASIC SUBCIRCUITS\n\n")
        basic_sc_file.write(".LIB BASIC_SUBCIRCUITS\n\n")
        basic_sc_file.close()

        # Generate wire subcircuit
        basic_subcircuits.wire_generate(self.basic_subcircuits_filename)
        # Generate pass-transistor subcircuit
        basic_subcircuits.ptran_generate(self.basic_subcircuits_filename, self.specs.use_finfet)
        basic_subcircuits.ptran_pmos_generate(self.basic_subcircuits_filename, self.specs.use_finfet)
        # Generate transmission gate subcircuit
        basic_subcircuits.tgate_generate(self.basic_subcircuits_filename, self.specs.use_finfet)
        basic_subcircuits.tgate_generate_lp(self.basic_subcircuits_filename, self.specs.use_finfet)
        # Generate level-restore subcircuit
        basic_subcircuits.rest_generate(self.basic_subcircuits_filename, self.specs.use_finfet)
        # Generate inverter subcircuit
        basic_subcircuits.inverter_generate(self.basic_subcircuits_filename, self.specs.use_finfet, self.specs.memory_technology)
        # Generate nand2
        basic_subcircuits.nand2_generate(self.basic_subcircuits_filename, self.specs.use_finfet)
        basic_subcircuits.nand2_generate_lp(self.basic_subcircuits_filename, self.specs.use_finfet)
        # Generate nand3 
        basic_subcircuits.nand3_generate(self.basic_subcircuits_filename, self.specs.use_finfet)
        basic_subcircuits.nand3_generate_lp(self.basic_subcircuits_filename, self.specs.use_finfet)
        #generate ram tgate
        basic_subcircuits.RAM_tgate_generate(self.basic_subcircuits_filename, self.specs.use_finfet)
        basic_subcircuits.RAM_tgate_generate_lp(self.basic_subcircuits_filename, self.specs.use_finfet)

        # Write footer
        basic_sc_file = open(self.basic_subcircuits_filename, 'a')
        basic_sc_file.write(".ENDL BASIC_SUBCIRCUITS")
        basic_sc_file.close()
        
        
    def _generate_process_data(self):
        """ Write the process data library file. It contains voltage levels, gate length and device models. """
        
        print "Generating process data file"

        
        process_data_file = open(self.process_data_filename, 'w')
        process_data_file.write("*** PROCESS DATA AND VOLTAGE LEVELS\n\n")
        process_data_file.write(".LIB PROCESS_DATA\n\n")
        process_data_file.write("* Voltage levels\n")
        process_data_file.write(".PARAM supply_v = " + str(self.specs.vdd) + "\n")
        process_data_file.write(".PARAM sram_v = " + str(self.specs.vsram) + "\n")
        process_data_file.write(".PARAM sram_n_v = " + str(self.specs.vsram_n) + "\n")
        process_data_file.write(".PARAM Rcurrent = " + str(self.specs.worst_read_current) + "\n")
        process_data_file.write(".PARAM supply_v_lp = " + str(self.specs.vdd_low_power) + "\n\n")


        if self.specs.memory_technology == "MTJ":
            process_data_file.write(".PARAM target_bl = " + str(0.04) + "\n\n")

        if use_lp_transistor == 0 :
            process_data_file.write(".PARAM sense_v = " + str(self.specs.vdd - self.specs.sense_dv) + "\n\n")
        else:
            process_data_file.write(".PARAM sense_v = " + str(self.specs.vdd_low_power - self.specs.sense_dv) + "\n\n")


        process_data_file.write(".PARAM mtj_worst_high = " + str(self.specs.MTJ_Rhigh_worstcase) + "\n")
        process_data_file.write(".PARAM mtj_worst_low = " + str(self.specs.MTJ_Rlow_worstcase) + "\n")
        process_data_file.write(".PARAM mtj_nominal_low = " + str(self.specs.MTJ_Rlow_nominal) + "\n\n")
        process_data_file.write(".PARAM mtj_nominal_high = " + str(6250) + "\n\n") 
        process_data_file.write(".PARAM vref = " + str(self.specs.vref) + "\n")
        process_data_file.write(".PARAM vclmp = " + str(self.specs.vclmp) + "\n")

        process_data_file.write("* Geometry\n")
        process_data_file.write(".PARAM gate_length = " + str(self.specs.gate_length) + "n\n")
        process_data_file.write(".PARAM trans_diffusion_length = " + str(self.specs.trans_diffusion_length) + "n\n")
        process_data_file.write(".PARAM min_tran_width = " + str(self.specs.min_tran_width) + "n\n")
        process_data_file.write(".param rest_length_factor=" + str(self.specs.rest_length_factor) + "\n")
        process_data_file.write("\n")

        process_data_file.write("* Supply voltage.\n")
        process_data_file.write("VSUPPLY vdd gnd supply_v\n")
        process_data_file.write("VSUPPLYLP vdd_lp gnd supply_v_lp\n")
        process_data_file.write("* SRAM voltages connecting to gates\n")
        process_data_file.write("VSRAM vsram gnd sram_v\n")
        process_data_file.write("VrefMTJn vrefmtj gnd vref\n")
        process_data_file.write("Vclmomtjn vclmpmtj gnd vclmp\n")
        process_data_file.write("VSRAM_N vsram_n gnd sram_n_v\n\n")
        process_data_file.write("* Device models\n")
        process_data_file.write(".LIB \"" + self.specs.model_path + "\" " + self.specs.model_library + "\n\n")
        process_data_file.write(".ENDL PROCESS_DATA")
        process_data_file.close()
        
        
    def _generate_includes(self):
        """ Generate the includes file. Top-level SPICE decks should only include this file. """
    
        print "Generating includes file"
    
        includes_file = open(self.includes_filename, 'w')
        includes_file.write("*** INCLUDE ALL LIBRARIES\n\n")
        includes_file.write(".LIB INCLUDES\n\n")
        includes_file.write("* Include process data (voltage levels, gate length and device models library)\n")
        includes_file.write(".LIB \"process_data.l\" PROCESS_DATA\n\n")
        includes_file.write("* Include transistor parameters\n")
        includes_file.write("* Include wire resistance and capacitance\n")
        #includes_file.write(".LIB \"wire_RC.l\" WIRE_RC\n\n")
        includes_file.write("* Include basic subcircuits\n")
        includes_file.write(".LIB \"basic_subcircuits.l\" BASIC_SUBCIRCUITS\n\n")
        includes_file.write("* Include subcircuits\n")
        includes_file.write(".LIB \"subcircuits.l\" SUBCIRCUITS\n\n")
        includes_file.write("* Include sweep data file for .DATA sweep analysis\n")
        includes_file.write(".INCLUDE \"sweep_data.l\"\n\n")
        includes_file.write(".ENDL INCLUDES")
        includes_file.close()
        
        
    def _generate_sweep_data(self):
        """ Create the sweep_data.l file that COFFE uses to perform 
            multi-variable HSPICE parameter sweeping. """

        sweep_data_file = open(self.sweep_data_filename, 'w')
        sweep_data_file.close()
        

    def _update_transistor_sizes(self, element_names, combo, use_finfet, inv_ratios=None):
        """ This function is used to update self.transistor_sizes for a particular transistor sizing combination.
            'element_names' is a list of elements (ptran, inv, etc.) that need their sizes updated.
            'combo' is a particular transistor sizing combination for the transistors in 'element_names'
            'inv_ratios' are the inverter P/N ratios for this transistor sizing combination.
            'combo' will typically describe only a small group of transistors. Other transistors retain their current size."""
        
        # We start by making a dictionary of the transistor sizes we need to update
        new_sizes = {}
        for i in range(len(combo)):
            element_name = element_names[i]
            # If it's a pass-transistor, we just add the NMOS size
            if "ptran_" in element_name:
                new_sizes[element_name + "_nmos"] = combo[i]
            # If it's a level-restorer, we just add the PMOS size
            elif "rest_" in element_name:
                new_sizes[element_name + "_pmos"] = combo[i]
            # If it's a transmission gate, we just add the PMOS and NMOS sizes
            elif "tgate_" in element_name:
                new_sizes[element_name + "_pmos"] = combo[i]
                new_sizes[element_name + "_nmos"] = combo[i]
            # If it's an inverter, we have to add both NMOS and PMOS sizes
            elif "inv_" in element_name:
                if inv_ratios == None:
                    # If no inverter ratios are specified, NMOS and PMOS are equal size
                    new_sizes[element_name + "_nmos"] = combo[i]
                    new_sizes[element_name + "_pmos"] = combo[i]
                else:
                    # If there are inverter ratios, we use them to give different sizes to NMOS and PMOS
                    if inv_ratios[element_name] < 1:
                        # NMOS is larger than PMOS
                        if not use_finfet:
                            new_sizes[element_name + "_nmos"] = combo[i]/inv_ratios[element_name]
                        else :
                            new_sizes[element_name + "_nmos"] = round(combo[i]/inv_ratios[element_name])
                            # new_sizes[element_name + "_nmos"] = combo[i]
                        new_sizes[element_name + "_pmos"] = combo[i]
                    else:
                        # PMOS is larger than NMOS
                        new_sizes[element_name + "_nmos"] = combo[i]
                        if not use_finfet :
                            new_sizes[element_name + "_pmos"] = combo[i]*inv_ratios[element_name]
                        else :
                            new_sizes[element_name + "_pmos"] = round(combo[i]*inv_ratios[element_name])
                            # new_sizes[element_name + "_pmos"] = combo[i]

        # Now, update self.transistor_sizes with these new sizes
        self.transistor_sizes.update(new_sizes)
      
      
    def _update_area_per_transistor(self):
        """ We use self.transistor_sizes to calculate area
            Using the area model, we calculate the transistor area in minimum width transistor areas.
            We also calculate area in nm and transistor width in nm. Nanometer values are needed for wire length calculations.
            For each transistor, this data forms a tuple (tran_name, tran_channel_width_nm, tran_drive_strength, tran_area_min_areas, tran_area_nm, tran_width_nm)
            The FPGAs transistor_area_list is updated once these values are computed."""
        
        # Initialize transistor area list
        tran_area_list = []
        
        # For each transistor, calculate area
        for tran_name, tran_size in self.transistor_sizes.iteritems():
                # Get transistor drive strength (drive strength is = xMin width)
                tran_drive = tran_size
                # Get tran area in min transistor widths
                tran_area = self._area_model(tran_name, tran_drive)
                # Get area in nm square
                tran_area_nm = tran_area*self.specs.min_width_tran_area
                # Get width of transistor in nm
                tran_width = math.sqrt(tran_area_nm)
                # Add this as a tuple to the tran_area_list
                # TODO: tran_size and tran_drive are the same thing?!
                tran_area_list.append((tran_name, tran_size, tran_drive, tran_area, 
                                                tran_area_nm, tran_width))
                                                
        # Assign list to FPGA object
        self.transistor_area_list = tran_area_list
        

    def _update_area_and_width_dicts(self):
        """ Calculate area for basic subcircuits like inverters, pass transistor, 
            transmission gates, etc. Update area_dict and width_dict with this data."""
        
        # Initialize component area list of tuples (component name, component are, component width)
        comp_area_list = []
        
        # Create a dictionary to store component sizes for multi-transistor components
        comp_dict = {}
        
        # For each transistor in the transistor_area_list
        # tran is a tuple having the following formate (tran_name, tran_channel_width_nm, 
        # tran_drive_strength, tran_area_min_areas, tran_area_nm, tran_width_nm)
        for tran in self.transistor_area_list:
            # those components should have an nmos and a pmos transistors in them
            if "inv_" in tran[0] or "tgate_" in tran[0]:
                # Get the component name; transistors full name example: inv_lut_out_buffer_2_nmos.
                # so the component name after the next two lines will be inv_lut_out_buffe_2.
                comp_name = tran[0].replace("_nmos", "")
                comp_name = comp_name.replace("_pmos", "")
                
                # If the component is already in the dictionary
                if comp_name in comp_dict:
                    if "_nmos" in tran[0]:
                        # tran[4] is tran_area_nm
                        comp_dict[comp_name]["nmos"] = tran[4]
                    else:
                        comp_dict[comp_name]["pmos"] = tran[4]
                        
                    # At this point we should have both NMOS and PMOS sizes in the dictionary
                    # We can calculate the area of the inverter or tgate by doing the sum
                    comp_area = comp_dict[comp_name]["nmos"] + comp_dict[comp_name]["pmos"]
                    comp_width = math.sqrt(comp_area)
                    comp_area_list.append((comp_name, comp_area, comp_width))                 
                else:
                    # Create a dict for this component to store nmos and pmos sizes
                    comp_area_dict = {}
                    # Add either the nmos or pmos item
                    if "_nmos" in tran[0]:
                        comp_area_dict["nmos"] = tran[4]
                    else:
                        comp_area_dict["pmos"] = tran[4]
                        
                    # Add this inverter to the inverter dictionary    
                    comp_dict[comp_name] = comp_area_dict
            # those components only have one transistor in them
            elif "ptran_" in tran[0] or "rest_" in tran[0] or "tran_" in tran[0]:   
                # Get the comp name
                comp_name = tran[0].replace("_nmos", "")
                comp_name = comp_name.replace("_pmos", "")               
                # Add this to comp_area_list directly
                comp_area_list.append((comp_name, tran[4], tran[5]))            
        
        # Convert comp_area_list to area_dict and width_dict
        area_dict = {}
        width_dict = {}
        for component in comp_area_list:
            area_dict[component[0]] = component[1]
            width_dict[component[0]] = component[2]
        
        # Set the FPGA object area and width dict
        self.area_dict = area_dict
        self.width_dict = width_dict
  
        return


    def _adds_to_critical_path(self, subcircuit):
        """ This function returns true if the given subcircuit
            delay should be added to the critical path, and false
            otherwise """

        BLE = self.logic_cluster.ble
        if subcircuit == BLE.lut:
            return False

        if self.specs.use_fluts:
            if subcircuit == BLE.fmux:
                return False

        if self.updates:
            if subcircuit == BLE.fmux_l2:
                return False

        if self.updates == 4:
            if subcircuit == BLE.fmux_l3:
                return False

        if self.specs.enable_carry_chain:
            if (subcircuit == self.carrychain or
                subcircuit == self.carrychainperf or
                subcircuit == self.carrychaininter): 
                return False
            if self.specs.carry_chain_type == "skip":
                if (subcircuit == self.carrychainand or
                    subcircuit == self.carrychainskipmux):
                    return False
            if not self.updates:
                if subcircuit == self.carrychainmux:
                    return False

        if self.updates in (2, 3):
            if (subcircuit == self.carrychain2 or
                subcircuit == self.carrychainperf2 or
                subcircuit == self.carrychaininter2): 
                return False
        
        #if self.updates in (1, 2, 4, 10):
        #    if (subcircuit == self.logic_cluster.ble.ff2.input_mux or
        #        subcircuit == self.logic_cluster.ble.ff3.input_mux):
        #        return False

        #if self.updates == 3:
        #    if (subcircuit == self.logic_cluster.ble.ff3.input_mux or
        #        subcircuit == self.logic_cluster.ble.ff4.input_mux):
        #        return False
        
        return True


