#!/usr/bin/env python3

"""Plasma GCode Preprocessor - process gcode files for qtpyvcp plasma_db usage

    Using the 'filter' program model of linuxcnc,
    processes the raw gcode file coming from a sheetcam
    or similar to a gcode file with material and path
    substitutions needed to support plasma_db plugin
    cut/material/process configs.
    The results are printed to standard-out. Other
    special case data (e.g. progress) is sent to
    standard-error

Usage:
  plasma_gcode_preprocessor <gcode-file>
  plasma_gcode_preprocessor -h

"""

import os
import sys
import re
import math
from enum import Enum, auto
from typing import List, Dict, Tuple, Union

import hal
import linuxcnc
from qtpyvcp.plugins.plasma_processes import PlasmaProcesses
from qtpyvcp.utilities.logger import initBaseLogger
from qtpyvcp.utilities.misc import normalizePath
from qtpyvcp.utilities.config_loader import load_config_files

#import pydevd;pydevd.settrace()


# Constrcut LOG from qtpyvcp standard logging framework
LOG = initBaseLogger('qtpyvcp.tools.plasma_gcode_preprocessor')
# Set over arching converstion fact. All thinking and calcs are in mm so need
# convert and arbirary values to bannas when they are in use
INI = linuxcnc.ini(os.environ['INI_FILE_NAME'])
UNITS, PRECISION, UNITS_PER_MM = ['in',6,25.4] if INI.find('TRAJ', 'LINEAR_UNITS') == 'inch' else ['mm',4,1]

# force the python HAL lib to load/init. Not doing this causes a silent "crash"
# when trying to set a hal pin
try:
    h = hal.component('dummy')
    LOG.debug('Python HAL is available')
except:
    LOG.warn('Python HAL is NOT available')


# Define some globals that will be referenced from anywhere
# assumption is MM's is the base unit of reference.
PLASMADB = None
DEBUG_COMMENTS = False

G_MODAL_GROUPS = {
    1: ('G0','G1','G2','G3','G33','G38.n','G73','G76','G80','G81',\
        'G82','G83','G84','G85','G86','G87','G88','G89'),
    2: ('G17','G18','G19','G17.1','G18.1','G19.1'),
    3: ('G90','G91'),
    4: ('G90.1','G91.1'),
    5: ('G93','G94','G95'),
    6: ('G20','G21'),
    7: ('G40','G41','G42','G41.1','G42.1'),
    8: ('G43','G43.1','G49'),
    10: ('G98','G99'),
    12: ('G54','G55','G56','G57','G58','G59','G59.1','G59.2','G59.3'),
    13: ('G61','G61.1','G64'),
    14: ('G96','G97'),
    15: ('G7','G8')}

M_MODAL_GROUPS = {
    4: ('M0','M1','M2','M30','M60'),
    7: ('M3','M4','M5'),
    9: ('M48','M49')}


# Enum for line type
class Commands(Enum):
    COMMENT                     = auto()
    CONTAINS_COMMENT            = auto()
    MOVE_LINEAR                 = auto()
    MOVE_ARC                    = auto()
    ARC_RELATIVE                = auto()
    ARC_ABSOLUTE                = auto()
    TOOLCHANGE                  = auto()
    PASSTHROUGH                 = auto()
    OTHER                       = auto()
    XY                          = auto()
    MAGIC_MATERIAL              = auto()
    MATERIAL_CHANGE             = auto()
    BEGIN_CUT                   = auto()
    END_CUT                     = auto()
    BEGIN_SCRIBE                = auto()
    END_SCRIBE                  = auto()
    BEGIN_SPOT                  = auto()
    END_SPOT                    = auto()
    BEGIN_MARK                  = auto()
    END_MARK                    = auto()
    END_ALL                     = auto()
    SELECT_PROCESS              = auto()
    WAIT_PROCESS                = auto()
    FEEDRATE_MATERIAL           = auto()
    FEEDRATE_LINE               = auto()
    ENABLE_IGNORE_ARC_OK_SYNCH  = auto()
    ENABLE_IGNORE_ARC_OK_IMMED  = auto()
    DISABLE_IGNORE_ARC_OK_SYNCH = auto()
    DISABLE_IGNORE_ARC_OK_IMMED = auto()
    DISABLE_THC_SYNCH           = auto()
    DISABLE_THC_IMMED           = auto()
    ENABLE_THC_SYNCH            = auto()
    ENABLE_THC_IMMED            = auto()
    DISABLE_TORCH_SYNCH         = auto()
    DISABLE_TORCH_IMMED         = auto()
    ENABLE_TORCH_SYNCH          = auto()
    ENABLE_TORCH_IMMED          = auto()
    FEED_VEL_PERCENT_SYNCH      = auto()
    FEED_VEL_PERCENT_IMMED      = auto()
    CUTTER_COMP_LEFT            = auto()
    CUTTER_COMP_RIGHT           = auto()
    CUTTER_COMP_OFF             = auto()
    HOLE_MODE                   = auto()
    HOLE_DIAM                   = auto()
    HOLE_VEL                    = auto()
    HOLE_OVERCUT                = auto()
    PIERCE_MODE                 = auto()
    KEEP_Z                      = auto()
    UNITS                       = auto()
    PATH_BLENDING               = auto()
    ADAPTIVE_FEED               = auto()
    SPINDLE_ON                  = auto()
    SPINDLE_OFF                 = auto()
    DIGITAL_IN                  = auto()
    REMOVE                      = auto()



class CodeLine:
# Class to represent a single line of gcode

    def __init__(self, line, parent = None):
        """args:
        line:  the gcode line to be parsed
        mode: the model state. i.e. what G state has been set
        """
        self._parent = parent
        self.command = ()
        self.params = {}
        self.comment = ''
        self.raw = line
        self.errors = {}
        self.type = None
        self.is_hole = False
        self.token = ''
        self.active_g_modal_groups = {}
        self.cutchart_id = None
        self.hole_builder = None


        # token mapping for line commands
        tokens = {
            'G0':(Commands.MOVE_LINEAR, self.parse_linear),
            'G1':(Commands.MOVE_LINEAR, self.parse_linear),
            'G20':(Commands.UNITS, self.set_inches),
            'G21':(Commands.UNITS, self.set_mms),
            'G2':(Commands.MOVE_ARC, self.parse_arc),
            'G3':(Commands.MOVE_ARC, self.parse_arc),
            #'M3$0':Commands.BEGIN_CUT,
            #'M5$0':Commands.END_CUT,
            #'M3$1':Commands.BEGIN_SCRIBE,
            #'M5$1':Commands.END_SCRIBE,
            #'M3$2':Commands.BEGIN_SPOT,
            #'M5$2':Commands.END_SPOT,
            #'M5$-1':Commands.END_ALL,
            #'M190':(Commands.SELECT_PROCESS, self.placeholder),
            #'M66P3L3':(Commands.WAIT_PROCESS, self.placeholder),
            #'F#<_hal[plasmac.cut-feed-rate]>':Commands.FEEDRATE_MATERIAL,
            #'M62P1':Commands.ENABLE_IGNORE_ARC_OK_SYNCH,
            #'M64P1':Commands.ENABLE_IGNORE_ARC_OK_IMMED,
            #'M63P1':Commands.DISABLE_IGNORE_ARC_OK_SYNCH,
            #'M65P1':Commands.DISABLE_IGNORE_ARC_OK_IMMED,
            #'M62P2':Commands.DISABLE_THC_SYNCH,
            #'M64P2':Commands.DISABLE_THC_IMMED,
            #'M63P2':Commands.ENABLE_THC_SYNCH,
            #'M65P2':Commands.ENABLE_THC_IMMED,
            #'M62P3':Commands.DISABLE_TORCH_SYNCH,
            #'M64P3':Commands.DISABLE_TORCH_IMMED,
            #'M63P3':Commands.ENABLE_TORCH_SYNCH,
            #'M65P3':Commands.ENABLE_TORCH_IMMED,
            #'M67E3':Commands.FEED_VEL_PERCENT_SYNCH,
            #'M68E3':Commands.FEED_VEL_PERCENT_IMMED,
            'G41':(Commands.CUTTER_COMP_LEFT, self.cutter_comp_error),
            'G42':(Commands.CUTTER_COMP_RIGHT, self.cutter_comp_error),
            'G41.1':(Commands.CUTTER_COMP_LEFT, self.cutter_comp_error),
            'G42.1':(Commands.CUTTER_COMP_RIGHT, self.cutter_comp_error),
            'G40':(Commands.CUTTER_COMP_OFF, self.placeholder),
            'G64':(Commands.PATH_BLENDING, self.parse_passthrough),
            'M52':(Commands.ADAPTIVE_FEED, self.parse_passthrough),
            'M3':(Commands.SPINDLE_ON, self.parse_passthrough),
            'M5':(Commands.SPINDLE_OFF, self.parse_passthrough),
            'M190':(Commands.MATERIAL_CHANGE, self.parse_passthrough),
            'M66':(Commands.DIGITAL_IN, self.parse_passthrough),
            'G91.1':(Commands.ARC_RELATIVE, self.parse_passthrough),
            'G90.1':(Commands.ARC_ABSOLUTE, self.parse_passthrough),
            'F#':(Commands.FEEDRATE_MATERIAL, self.parse_passthrough),
            'F':(Commands.FEEDRATE_LINE, self.parse_feedrate),
            '#<holes>':(Commands.HOLE_MODE, self.placeholder),
            '#<h_diameter>':(Commands.HOLE_DIAM, self.placeholder),
            '#<h_velocity>':(Commands.HOLE_VEL, self.placeholder),
            '#<oclength>':(Commands.HOLE_OVERCUT, self.placeholder),
            '#<pierce-only>':(Commands.PIERCE_MODE, self.placeholder),
            #'#<keep-z-motion>':Commands.KEEP_Z,
            ';':(Commands.COMMENT, self.parse_comment),
            '(':(Commands.COMMENT, self.parse_comment),
            'T':(Commands.TOOLCHANGE, self.parse_toolchange)
            #'(o=':Commands.MAGIC_MATERIAL
            }

        # a line could have multiple Gcodes on it. This is typical of the
        # preamble set by many CAM packages.  The processor should not need
        # to change any of this. It should be 'correct'.  So all we need
        # to do is
        # [1] Recognise it is there
        # [2] Scan for any illegal codes, set any error codes if needed
        # [3] Mark line for pass through
        multi_codes = re.findall(r"G\d+|T\s*\d+|M\d+", line.upper().strip())
        if len(multi_codes) > 1:
            # we have multiple codes on the line
            self.type = Commands.PASSTHROUGH
            # scan for possible 'bad' codes
            for code in multi_codes:
                if code in ('G41','G42','G41.1','G42.1'):
                    # we have an error state
                    self.cutter_comp_error()
            # look for Tx M6 combo
            f = re.findall("T\s*\d+|M6", line.upper().strip())
            if len(f) == 2:
                # we have a tool change combo. Assume in form Tx M6
                self.parse_toolchange(combo=True)
        else:
            # not a multi code on single line situation so process line
            # to set line type
            for k in tokens:
                # do regex searches to find exact matches of the token patterns
                pattern = r"^"+k + r"{1}"
                if k == '(':
                    # deal with escaping the '(' which is special char in regex
                    pattern = r"^\({1}"
                r = re.search(pattern, line.upper())
                if r != None:
                    # since r is NOT None we must have found something
                    self.type = tokens[k][0]
                    self.token = k
                    # call the parser method bound to this key
                    tokens[k][1]()
                    # check for an inline comment if the entire line is not a comment
                    if self.type is not Commands.COMMENT:
                        self.parse_inline_comment()
                    # break out of the loop, we found a match
                    break
                else:
                    # nothing of interest just mark the line for pass through processing
                    self.type = Commands.PASSTHROUGH
            if self.type is Commands.PASSTHROUGH:
                # If the result was seen as 'OTHER' do some further checks
                # As soon as we shift off being type OTHER, exit the method
                # 1. is it an XY line
                self.parse_XY_line()
                #if self.type is not Commands.OTHER: return


    def strip_inline_comment(self, line):
        s = re.split(";|\(", line, 1)
        try:
            return s[0].strip()
        except:
            return line

    def save_g_modal_group(self, grp):
        self.active_g_modal_groups = grp.copy()


    def parse_comment(self):
        self.comment = self.raw
        self.command = (';', None)
        self.params = {}


    def parse_inline_comment(self):
        # look for possible inline comment
        s = re.split(";|\(",self.raw[1:],1)
        robj = re.search(";|\(",self.raw[1:])
        if robj != None:
            # found an inline comment. get the char token for the comment
            i = robj.start() + 1
            found_c = self.raw[i]
            self.comment = found_c + s[1]
        else:
            # no comment found to empty the char token var
            found_c = ''
            self.comment = ''


    def parse_other(self):
        LOG.debug(f'Type OTHER -- {self.token} -- found - this code is not handled or considered')
        pass

    def parse_passthrough(self):
        self.type = Commands.PASSTHROUGH

    def parse_remove(self):
        self.type = Commands.REMOVE

    def parse_linear(self):
        # linear motion means either G0 or G1. So looking for X/Y on this line
        self.command = ('G',int(self.token[1:]))
        # split the raw line at the token and then look for X/Y existence
        line = self.raw.upper().split(self.token,1)[1].strip()
        tokens = re.finditer(r"X[\d\+\.-]*|Y[\d\+\.-]*", line)
        for token in tokens:
            params = re.findall(r"X|Y|[\d\+\.-]+", token.group())
            # this is now a list which can be added to the params dictionary
            if len(params) == 2:
                self.params[params[0]] = float(params[1])


    def parse_XY_line(self):
        line = self.raw.upper().strip()
        tokens = re.finditer(r"X[\d\+\.-]*|Y[\d\+\.-]*", line)
        for token in tokens:
            params = re.findall(r"X|Y|[\d\+\.-]+", token.group())
            # this is now a list which can be added to the params dictionary
            if len(params) == 2:
                self.params[params[0]] = float(params[1])
            # if we are in the loop then we found X/Y instances so mark the line type
            self.type = Commands.XY


    def parse_arc(self):
        # arc motion means either G2 or G3. So looking for X/Y/I/J/P on this line
        self.command = ('G',int(self.token[1:]))
        # split the raw line at the token and then look for X/Y/I/J/P existence
        line = self.strip_inline_comment(self.raw).upper().split(self.token,1)[1].strip()
        tokens = re.finditer(r"X[\d\+\.-]*|Y[\d\+\.-]*|I[\d\+\.-]*|J[\d\+\.-]*|P[\d\+\.-]*", line)
        for token in tokens:
            params = re.findall(r"X|Y|I|J|P|[\d\+\.-]+", token.group())
            # this is now a list which can be added to the params dictionary
            if len(params) == 2:
                self.params[params[0]] = float(params[1])


    def parse_toolchange(self, combo=False):
        # A tool change is deemed a process change.
        # The Tool Number is to be the unique ID of a Process combination from
        # the CustChart table. This will need to be supported by the
        # CAM having a loading of tools where the tool #  is the ID from this table
        # Param: combo - if True then line has both Tx and M6
        line = self.strip_inline_comment(self.raw)
        if combo:
            f = re.findall(r"T\s*\d+|M6", line.upper().strip())
            # assume is in format Tx M6
            tool = int(re.split('T', f[0], 1)[1])
            self.type = Commands.PASSTHROUGH
        else:
            tool = int(re.split('T', line, 1)[1])
            self.command = ('T',tool)
            self.type = Commands.TOOLCHANGE
        # test if this process ID is known about
        cut_process = PLASMADB.cut_by_id(tool)
        if len(cut_process) == 0:
            # rewrite the raw line as an error comment
            self.raw = f"; ERROR: Invalid Cutchart ID in Tx. Check CAM Tools: {self.raw}"
            LOG.warn(f'Tool {tool} not a valid cut process in DB')
        else:
            self.cutchart_id = tool
            self._parent.active_cutchart = tool
            self._parent.active_feedrate = cut_process[0].cut_speed
            self._parent.active_thickness = cut_process[0].thickness.thickness
            self._parent.active_machineid = cut_process[0].machineid
            self._parent.active_thicknessid = cut_process[0].thicknessid
            self._parent.active_materialid = cut_process[0].materialid
            

    def parse_feedrate(self):
        # assumption is that the feed is on its own line
        line = self.strip_inline_comment(self.raw)
        feed = float(re.split('F', line, 1)[1])
        if self._parent.active_feedrate is not None:
            feed = self._parent.active_feedrate
        self.command = ('F', feed)


    def set_inches(self):
        global UNITS_PER_MM
        UNITS_PER_MM = 25.4
        self.command = ('G', 20)


    def set_mms(self):
        global UNITS_PER_MM
        UNITS_PER_MM = 1
        self.command = ('G', 21)


    def cutter_comp_error(self):
        # cutter compensation detected.  Not supported by processor
        self.errors['compError'] = "Cutter compensation detected. \
                                    Ensure all compensation is baked into the tool path."
        print(f'ERROR:CUTTER_COMP:INVALID GCODE FOUND',file=sys.stderr)
        sys.stderr.flush()
        self.type = Commands.REMOVE

    def get_active_feedrate(self):
        return self._parent.active_feedrate

    def placeholder(self):
        LOG.debug(f'Type PLACEHOLDER -- {self.token} -- found - this code is not handled or considered')
        pass


class HoleBuilder:
    def __init__(self):
        torch_on = False
        self.elements = []

    def degrees(self, rad):
        #convert radians to degrees to help decipher the angles
        return(rad *(180/math.pi))

    def line_length(self, x1, y1, x2, y2):
        a = (x2 - x1)**2
        b = (y2 - y1)**2
        s = a + b
        rtn = math.sqrt(s)
        return rtn

    def create_ccw_arc_gcode(self, x, y, rx, ry):
        return {
            "code": "G3",
            "x": x,
            "y": y,
            "i": rx,
            "j": ry
        }

    def create_cw_arc_gcode(self, x, y, rx, ry):
        return {
            "code": "G2",
            "x": x,
            "y": y,
            "i": rx,
            "j": ry
        }

    def create_line_gcode(self, x, y, rapid):
        return {
            "code": "G0" if rapid else "G1",
            "x": x,
            "y": y
        }

    def create_cut_on_off_gcode(self, cut_on, spindle=0):
        self.torch_on = cut_on
        return {
            "code": f"M3 ${spindle}" if cut_on else "M5 $-1"
        }

    def create_kerf_off_gcode(self):
        return {
            "code": "G40"
        }

    def create_comment(self, txt):
        return {
            "code": f"({txt})"
        }
        
    def create_debug_comment(self, txt):
        return {
            "code": f"{txt}" if DEBUG_COMMENTS else None
        }

    def create_dwell(self, t):
        # add a G4 Pn dwell between segments
        return {
             "code": f"G4 P{t}"
        }
        
    def create_feed(self, r):
        return {
            "code": f"F{r}"
        }
    
    def create_absolute_arc(self):
        return {
            "code": "G90.1"
        }

    def create_relative_arc(self):
        return {
            "code": "G91.1"
        }

    def create_thc_off_synch(self):
        return {
            "code": "M62 P2"
        }


    def create_thc_on_synch(self):
        return {
            "code": "M63 P2"
        }
        
    def create_marking_voltage_wait(self):
        return {
            # Wait on digital pin 4 to go HIGH
            "code": "M66 P3 L3 Q10"
        }


    def element_to_gcode_line(self, e):
        if PRECISION == 4:
            xy = '{} x{:.4f} y{:.4f}'
            ij = ' i{:.4f} j{:.4f}'
        else:
            xy = '{} x{:.6f} y{:.6f}'
            ij = ' i{:.6f} j{:.6f}'
        
        if 'i' in e:
            line = (xy + ij).format(e['code'], e['x'], e['y'], e['i'], e['j'])
        elif 'x' in e:
            line = xy.format(e['code'], e['x'], e['y'])
        else:
            line = e['code']
        return line

    def plasma_mark(self, line, x, y, time):
        self.elements=[]
        feed_rate = line.get_active_feedrate()
        self.elements.append(self.create_comment('---- Marking Start ----'))
        self.elements.append(self.create_feed(feed_rate))
        self.elements.append(self.create_line_gcode(x, y, True))
        self.elements.append(self.create_cut_on_off_gcode(True))
        self.elements.append(self.create_line_gcode(x+0.001, y, False))
        self.elements.append(self.create_marking_voltage_wait())
        self.elements.append(self.create_dwell(time))
        self.elements.append(self.create_cut_on_off_gcode(False))
        self.elements.append(self.create_comment('---- Marking End ----'))

    def plasma_hole(self, line, x, y, d, kerf, leadin_radius, splits=[], hidef=False):
        # Params:
        # x:              Hole Centre X position
        # y:              Hole Centre y position
        # d:              Hole diameter
        # kerf:           Kerf width for cut
        # leadin_radius:  Radius for the lead in arc
        # splits[]:       List of length segments. Segments will support different speeds
        #                 and starting positions of the circle. Including overburn
        LOG.debug('Build smart hole')
        #kerf compensation
        # often code is already compensated. We need to be able to tell the script if it is
        #changed the radius parameter to be ad diameter which is more in keeping with the hole data methodology
        feed_rate = line.get_active_feedrate()
        arc1_feed = feed_rate * hal.get_value('qtpyvcp.plasma-arc1-percent.out')/100
        arc2_feed = feed_rate * hal.get_value('qtpyvcp.plasma-arc2-percent.out')/100
        arc3_feed = feed_rate * hal.get_value('qtpyvcp.plasma-arc3-percent.out')/100
        leadin_feed = feed_rate * hal.get_value('qtpyvcp.plasma-leadin-percent.out')/100

        # is G40 oavtive or not
        if line.active_g_modal_groups[7] == 'G40':
            g40 = True
        else:
            g40 = False

        r = float(d)/2.00
        kc = float(kerf) / 2
        # kerf larger than hole -> hole disappears
        # Mark that hole has been ignored with a comment.
        if kc > r:
            self.elements = []
            self.elements.append(self.create_comment('1/2 Kerf > Hole Radius.  Smart Hole processing skipped.'))
            return

        # convert split distances to angles (in radians)
        split_angles = []
        full_circle = math.pi * 2  # 360 degrees. We need to use this for a segment moving to 12 O'clock
        degs90 = math.pi / 2
        crossed_origin = False
        for spt in splits:
            # using the relationship of arc_length/circumfrance = angle/360 if you work the algebra you find:
            # angle = arc_length/radius (in radians)
            tmp_ang = float(spt) / float(r)     # need to typecast to prevent Python from truncating to whole values, maybe should be double?
            this_ang = tmp_ang    # Accoding to Juha, all angles are from 0 degrees, -ve angles to the right, +ve angles to the left
            # this_ang = full_circle + tmp_ang    # Accoding to Juha, all angles are from 0 degrees, -ve angles to the right, +ve angles to the left
            # if (this_ang > full_circle) and (crossed_origin == False):
            #     # Has this split got to the 0deg/360 deg  origin?
            #     # when we cross the origin, add a 360 degree keep it in the correct order
            #     split_angles.append(full_circle)
            #     this_ang = tmp_ang
            #     crossed_origin = True
            split_angles.append(this_ang)
        #Sorting is not helpful with splits becasue the smaller values come befor ethe first segment.
        #We need to keep ssegments in order
        #sort angles, smallest first

        # compensate hole radius and leadin radius if not already compensated code
        # Testing for g40 active.  HOWEVER using a G41/42 code causes so many lost plasmac featrues
        # why on earth would you use it?
        r = r if g40 else r - kc
        leadin_radius = leadin_radius if g40 else leadin_radius -kc

        # the first real point of the hole (after leadin)
        arc_x0 = x
        arc_y0 = y + r

        # make sure gcode elements list is empty
        self.elements = []

        if line.active_g_modal_groups[4] == 'G91.1':
            self.elements.append(self.create_absolute_arc())
        if hidef:
            self.elements.append(self.create_comment('---- HiDef Hole ----'))
        self.elements.append(self.create_debug_comment(f'Hole Center x={x} y={y} r={r} leadin_r={leadin_radius}'))
        self.elements.append(self.create_debug_comment(f'First point on hole: x={arc_x0} y={arc_y0}'))
        self.elements.append(self.create_debug_comment('Leadin...'))

        centre_to_leadin_diam_gap = math.fabs(arc_y0 - (2 * leadin_radius) - y)
        
        # set the lead in speed
        self.elements.append(self.create_feed(leadin_feed))
        # turn off the THC
        self.elements.append(self.create_thc_off_synch())

        # leadin radius too small or greater (or equal) than r.
        # --> use straight leadin from the hole center.
        if leadin_radius < 0 or leadin_radius >= r-kc:
            self.elements.append(self.create_debug_comment('too small'))
            self.elements.append(self.create_line_gcode(x, y, True))
            self.elements.append(self.create_kerf_off_gcode())
            # TORCH ON
            self.elements.append(self.create_cut_on_off_gcode(True))
            self.elements.append(self.create_line_gcode(arc_x0, arc_y0, False))

        # leadin radius <= r / 2.
        # --> use half circle leadin
        # done nothing here
        elif leadin_radius <= (r / 2):
            self.elements.append(self.create_debug_comment('Half circle radius'))
            # rapid to hole centre
            self.elements.append(self.create_debug_comment(f'Half circle radius. Centre-to-Leadin-Gap={centre_to_leadin_diam_gap}'))
            if centre_to_leadin_diam_gap < kerf:
                self.elements.append(self.create_debug_comment('... single arc'))
                self.elements.append(self.create_line_gcode(x, y, True))
                self.elements.append(self.create_kerf_off_gcode())
                # TORCH ON
                self.elements.append(self.create_cut_on_off_gcode(True))
                self.elements.append(self.create_line_gcode(x, arc_y0 - 2 * leadin_radius, False))
                self.elements.append(self.create_ccw_arc_gcode(arc_x0, arc_y0, x, arc_y0 - leadin_radius))
            else:
                self.elements.append(self.create_debug_comment('... double back arc'))
                self.elements.append(self.create_line_gcode(x, y, True))
                self.elements.append(self.create_kerf_off_gcode())
                # TORCH ON
                self.elements.append(self.create_cut_on_off_gcode(True))
                self.elements.append(self.create_cw_arc_gcode(x, y + centre_to_leadin_diam_gap, \
                                                              x, y + centre_to_leadin_diam_gap/2))
                self.elements.append(self.create_ccw_arc_gcode(arc_x0, arc_y0, x, arc_y0 - leadin_radius))
            

        # r/2 < leadin radius < r.
        # --> use combination of leadin arc and a smaller arc from the hole center
        else:
            # TODO:
            self.elements.append(self.create_debug_comment('Greater then Half circle radius'))
            self.elements.append(self.create_debug_comment(f'Half circle radius. Centre-to-Leadin-Gap={centre_to_leadin_diam_gap}'))

            if centre_to_leadin_diam_gap < kerf:
                self.elements.append(self.create_debug_comment('... single arc'))
                self.elements.append(self.create_line_gcode(x, y, True))
                self.elements.append(self.create_kerf_off_gcode())
                # TORCH ON
                self.elements.append(self.create_cut_on_off_gcode(True))
                self.elements.append(self.create_line_gcode(x, y - centre_to_leadin_diam_gap, False))
                self.elements.append(self.create_ccw_arc_gcode(arc_x0, arc_y0, x, arc_y0 - leadin_radius))
            else:
                self.elements.append(self.create_debug_comment('... double back arc'))
                self.elements.append(self.create_line_gcode(x, y, True))
                self.elements.append(self.create_kerf_off_gcode())
                # TORCH ON
                self.elements.append(self.create_cut_on_off_gcode(True))
                self.elements.append(self.create_ccw_arc_gcode(x, y - centre_to_leadin_diam_gap, \
                                                              x, y - centre_to_leadin_diam_gap/2))
                self.elements.append(self.create_ccw_arc_gcode(arc_x0, arc_y0, x, arc_y0 - leadin_radius))


            
            
            #leadin_diameter =  (leadin_radius + kc) * 2
            #from_centre = leadin_diameter - (r + kc)  # distance from centre
            # always start from centre of the hole
            #start1_x = x
            #startl_y = y
            #start1_y = r + from_centre  + kc # Y coordinate of start position
            
            # rapid to hole centre
            #self.elements.append(self.create_line_gcode(x, y , True))
            
            # self.elements.append(self.create_line_gcode(x, y-d/2 , True))
            # if abs(from_centre) < (kerf * 2):
            #      # no room for arc, use straight segment from hole centre
            #     self.elements.append(self.create_comment('no room for arc, use straight segment from hole centre...'))
            #     self.elements.append(self.create_line_gcode(start1_x, start1_y, False))
            # elif start1_y < r:
            #     # leadin diameter is shorter than hole radius, Use G2 for first arc
            #     self.elements.append(self.create_comment('leadin diameter is shorter than hole radius, Use G2 for first arc...'))
            #     self.elements.append(self.create_cw_arc_gcode(start1_x , start1_y, x, start1_y - from_centre))
            # else:
            #     #leadin diameter is longer than hole radius, Use G3 for first arc
            #     self.elements.append(self.create_comment('leadin diameter is longer than hole radius, Use G3 for first arc...'))
            #     self.elements.append(self.create_ccw_arc_gcode(x , -(leadin_diameter) , x, -(start1_y - from_centre/2)))
            # self.elements.append(self.create_ccw_arc_gcode(x , y - kc, x, -(leadin_diameter-(leadin_diameter - kc)/2)))

        self.elements.append(self.create_comment('Hole...'))
        #this has been reworked quite a bit. The original code was referring to the cursor X & Y positions and they needed to be the hole centre
        # TODO: not happy with this as it only works where x = 0 I think. Nees to be more robust
        cx = x
        cy =  y

        if len(split_angles) > 0:
            sector_num = 0
            for sang in split_angles:
                end_angle = sang
                end_x = ( cx + r * math.cos(end_angle + degs90))
                end_y = ( cy + r * math.sin(end_angle + degs90))
                if sang == full_circle or sang == 0:
                    #reset coordinates to 0,0 if angle = 360 degrees. We want the next segments to refer to 0 degrees
                    end_x = x
                    end_y = y + r
                # if sang < split_angles[0] and sang > 0.00:
                #     #conditional to coordinate positive angles
                #     end_x = (cx - r * math.cos(end_angle + degs90))
                #     end_y = (cy - r * math.sin(end_angle + degs90))
                #comment the code
                self.elements.append(self.create_debug_comment(f'Settings: angle = {str(sang)} end_angle {str(end_angle)} radians {str(self.degrees(end_angle))} degrees'))
                self.elements.append(self.create_debug_comment(f'Arc length = {r * sang}'))
                self.elements.append(self.create_comment(f'Sector number: {sector_num}'))
                if sector_num == 0:
                    self.elements.append(self.create_feed(arc1_feed))
                elif sector_num == 1:
                    self.elements.append(self.create_feed(arc2_feed))
                elif sector_num == 2:
                    # TORCH OFF
                    self.elements.append(self.create_cut_on_off_gcode(False))
                    self.elements.append(self.create_feed(arc3_feed))
                self.elements.append(self.create_ccw_arc_gcode(end_x, end_y, cx, cy))
                # if (end_x == 0.00 and end_y == 0.00) == False:
                #     #if not 12 O'clock, dwell for 0.5 sec so we have a visual indicator of each segment
                #     # we need to insert a call to a procedure that creates the required gcode actions at the end of each segment
                #     self.elements.append(self.create_dwell('0.5'))
                sector_num += 1
        else:
            # create hole as four arcs. no overburn or anything special.
            self.elements.append(self.create_ccw_arc_gcode(x-r, y, x, y))
            self.elements.append(self.create_ccw_arc_gcode(x, y-r, x, y))
            self.elements.append(self.create_ccw_arc_gcode(x+r, y, x, y))
            self.elements.append(self.create_ccw_arc_gcode(x, y+r, x, y))

        # TORCH OFF
        if self.torch_on:
            self.elements.append(self.create_cut_on_off_gcode(False))
        # turn on the THC
        self.elements.append(self.create_thc_on_synch())
        # rest feed rate
        self.elements.append(self.create_feed(feed_rate))
        if line.active_g_modal_groups[4] == 'G91.1':
            self.elements.append(self.create_relative_arc())

    def generate_hole_gcode(self):
        for e in self.elements:
            if e['code'] is not None:
                print(self.element_to_gcode_line(e), file=sys.stdout)
                sys.stdout.flush()

class HiDefHole:
    def __init__(self, data_list):
        self.hole_list = []
        for d in data_list:
            self.hole_list.append({'hole': d.hole_size, \
                                   'leadinradius': d.leadin_radius, \
                                   'kerf': d.kerf, \
                                   'cutheight': d.cut_height, \
                                   'speed1': d.speed1, \
                                   'speed2': d.speed2, \
                                   'speed2dist': d.speed2_distance, \
                                   'offdistance': d.plasma_off_distance, \
                                   'overcut': d.over_cut, \
                                   'amps': d.amps
                                   })
        for i in range(len(self.hole_list)):
            # calculate the scale factors to use.
            # Factors are scaled over the diameter range of the hole
            if i > 0:
                delta = self.hole_list[i]['hole'] - self.hole_list[i-1]['hole']
                self.hole_list[i]['scale_leadinradius'] = self.hole_list[i]['leadinradius']/delta
                self.hole_list[i]['scale_kerf'] = self.hole_list[i]['kerf']/delta
                self.hole_list[i]['scale_cutheight'] = self.hole_list[i]['cutheight']/delta
                self.hole_list[i]['scale_speed1'] = self.hole_list[i]['speed1']/delta
                self.hole_list[i]['scale_speed2'] = self.hole_list[i]['speed2']/delta
                self.hole_list[i]['scale_speed2dist'] = self.hole_list[i]['speed2dist']/delta
                self.hole_list[i]['scale_offdistance'] = self.hole_list[i]['offdistance']/delta
                self.hole_list[i]['scale_overcut'] = self.hole_list[i]['overcut']/delta

    def get_attribute(self, attribute, holesize):
        """
        Valid attributes:
            leadinradius
            kerf
            cutheight
            speed1
            speed2
            speed2dist
            offdistance
            overcut
        """
        for i in range(1, len(self.hole_list)):
            if self.hole_list[i-1]['hole'] <= holesize and holesize <= self.hole_list[i]['hole']:
                lr = holesize * self.hole_list[i][f'scale_{attribute}']
                return lr
        return None


    def leadin_radius(self, holesize):
        return self.get_attribute('leadinradius', holesize)
    
    def kerf(self, holesize):
        return self.get_attribute('kerf', holesize)
    
    def cut_height(self, holesize):
        return self.get_attribute('cutheight', holesize)
    
    def speed1(self, holesize):
        return self.get_attribute('speed1', holesize)
    
    def speed2(self, holesize):
        return self.get_attribute('speed2', holesize)
    
    def speed2_distance(self, holesize):
        return self.get_attribute('speed2dist', holesize)
    
    def plasma_off_distance(self, holesize):
        return self.get_attribute('offdistance', holesize)
    
    def overcut(self, holesize):
        return self.get_attribute('overcut', holesize)
    

class PreProcessor:
    def __init__(self, inCode):
        self._new_gcode = []
        self._parsed = []
        self._line = ''
        self._line_num = 0
        self._line_type = 0
        self._orig_gcode = None
        self.active_g_modal_grps = {}
        self.active_m_modal_grps = {}
        self.active_cutchart = None
        self.active_feedrate = None
        self.active_thickness = None
        self.active_machineid = None
        self.active_thicknessid = None
        self.active_materialid = None
        

        openfile= open(inCode, 'r')
        self._orig_gcode = openfile.readlines()
        openfile.close()

    def set_active_g_modal(self, gcode):
        # get the modal grp for the code and set things
        # if a code is not found then nothing will be set
        for g_modal_grp in G_MODAL_GROUPS:
            if gcode in G_MODAL_GROUPS[g_modal_grp]:
                self.active_g_modal_grps[g_modal_grp] = gcode
                break


    def active_motion_code(self):
        try:
            return self.active_g_modal_grps[1]
        except:
            return None


    def flag_holes(self):
        # connect to HAL and collect the data we need to determine what holes
        # should be processes and what are too large
        thickness_ratio = hal.get_value('qtpyvcp.plasma-hole-thickness-ratio.out')
        max_hole_size = hal.get_value('qtpyvcp.plasma-max-hole-size.out')/UNITS_PER_MM
        
        arc1_feed_percent = hal.get_value('qtpyvcp.plasma-arc1-percent.out')/100
        
        arc2_distance = hal.get_value('qtpyvcp.plasma-arc2-distance.out')/UNITS_PER_MM
        arc2_feed_percent = hal.get_value('qtpyvcp.plasma-arc2-percent.out')/100
        
        arc3_distance = hal.get_value('qtpyvcp.plasma-arc3-distance.out')/UNITS_PER_MM
        arc3_feed_percent = hal.get_value('qtpyvcp.plasma-arc3-percent.out')/100
        
        leadin_feed_percent = hal.get_value('qtpyvcp.plasma-leadin-percent.out')/100
        leadin_radius = hal.get_value('qtpyvcp.plasma-leadin-radius.out')/UNITS_PER_MM
        
        kerf_width = hal.get_value('qtpyvcp.param-kirfwidth.out')/UNITS_PER_MM

        torch_off_distance_before_zero = hal.get_value('qtpyvcp.plasma-torch-off-distance.out')/UNITS_PER_MM
        
        small_hole_size = 0
        small_hole_detect = hal.get_value('qtpyvcp.plasma-small-hole-detect.checked')
        if small_hole_detect:
            small_hole_size = hal.get_value('qtpyvcp.plasma-small-hole-threshold.out')/UNITS_PER_MM
            
        marking_voltage = hal.get_value('qtpyvcp.spot-threshold.out')
        marking_delay = hal.get_value('qtpyvcp.spot-delay.out')
        
        
        # old school loop so we can easily peek forward or back of the current
        # record being processed.
        i = 0
        while i < len(self._parsed):
            line = self._parsed[i]
            if len(line.command) == 2:
                if line.command[0] == 'G' and line.command[1] == 3:
                    # this could be a hole, test for it.
                    # NB: Only circles that are defined as cww are deemed to be
                    # a hole.  cw (G2) cuts are deemed as an outer edge not inner.
                                        
                    #[1] find the last X and Y position while grp 1 was either G0 or G1
                    j = i-1
                    for j in range(j, -1, -1):
                        prev = self._parsed[j]
                        # is there an X or Y in the line
                        if 'X' in prev.params.keys() and prev.active_g_modal_groups[1] in ('G0','G1','G2','G3'):
                            lastx = prev.params['X']
                            break
                    j = i-1
                    for j in range(j, -1, -1):
                        prev = self._parsed[j]
                        # is there an X or Y in the line
                        if 'Y' in prev.params.keys() and prev.active_g_modal_groups[1] in ('G0','G1','G2','G3'):
                            lasty = prev.params['Y']
                            break
                    endx = line.params['X'] if 'X' in line.params.keys() else lastx
                    endy = line.params['Y'] if 'Y' in line.params.keys() else lasty
                    if endx == lastx and endy == lasty:
                        line.is_hole = True
                    else:
                        line.is_hole = False

                    # if line is a hole then prepare to replace
                    # with "smart" holes IF it is within the upper params of a
                    # hole definition.  Nomally <= 5 * thickness
                    if line.is_hole:
                        line.hole_builder = HoleBuilder()
                        arc_i = line.params['I']
                        arc_j = line.params['J']
                        centre_x = endx + arc_i
                        centre_y = endy + arc_j
                        radius = line.hole_builder.line_length(centre_x, centre_y,endx, endy)
                        diameter = 2 * math.fabs(radius)
                        circumferance = diameter * math.pi
                        
                        # see if can find hidef data for this hole scenario
                        hidef_data = PLASMADB.hidef_holes(self.active_machineid, self.active_materialid, self.active_thicknessid)
                        hidef = False
                        if len(hidef_data) > 0:
                            # leadinradius
                            # kerf
                            # cutheight
                            # speed1
                            # speed2
                            # speed2dist
                            # offdistance
                            # overcut
                            hidef_hole = HiDefHole(hidef_data)
                            hidef_leadin = hidef_hole.leadin_radius(diameter)
                            hidef_kerf = hidef_hole.kerf(diameter)
                            hidef_cutheight = hidef_hole.cut_height(diameter)
                            hidef_speed1 = hidef_hole.speed1(diameter)
                            hidef_speed2 = hidef_hole.speed2(diameter)
                            hidef_speed2dist = hidef_hole.speed2_distance(diameter)
                            hidef_offdistance = hidef_hole.plasma_off_distance(diameter)
                            hidef_overcut = hidef_hole.overcut(diameter)
                            if None not in (hidef_leadin, hidef_kerf, \
                                            hidef_cutheight, hidef_speed1, \
                                            hidef_speed2, hidef_speed2dist, \
                                            hidef_offdistance, hidef_overcut):
                                hidef = True
                        
                        if diameter < small_hole_size and small_hole_detect:
                            # removde the hole and replace with a pulse
                            line.hole_builder.\
                                plasma_mark(line, centre_x, centre_y, marking_delay)
                            # scan forward and back to mark the M3 and M5 as Coammands.REMOVE
                            j = i-1
                            found_m3 = False
                            for j in range(j, -1, -1):
                                prev = self._parsed[j]
                                # mark for removal any lines until find the M3
                                if prev.token.startswith('M3'):
                                    found_m3 = True
                                    prev.type = Commands.REMOVE
                                if not found_m3:
                                    prev.type = Commands.REMOVE
                                try:
                                    if prev.active_g_modal_groups[1] != 'G0' and found_m3:
                                        break
                                    elif prev.active_g_modal_groups[1] == 'G0':
                                        prev.type = Commands.REMOVE
                                except KeyError:
                                    # access to the dictionary index failed,
                                    # so no longer in a g0 mode
                                    break
                            j = i+1
                            for j in range(j, len(self._parsed)):
                                next = self._parsed[j]
                                # mark all lines for removal until find M5
                                next.type = Commands.REMOVE
                                if next.token.startswith('M5'):
                                    next.type = Commands.REMOVE
                                    break
                        elif hidef:
                            arc1_distance = circumferance - hidef_speed2dist - hidef_offdistance
                            arc2_from_zero = arc1_distance + hidef_speed2dist
                            arc3_from_zero = arc2_from_zero + hidef_overcut - circumferance
                            line.hole_builder.\
                                plasma_hole(line, centre_x, centre_y, diameter, \
                                            hidef_kerf, hidef_leadin, \
                                            [arc1_distance, \
                                             arc2_from_zero, \
                                             arc3_from_zero], hidef)
                            
                            # scan forward and back to mark the M3 and M5 as Coammands.REMOVE
                            j = i-1
                            found_m3 = False
                            for j in range(j, -1, -1):
                                prev = self._parsed[j]
                                # mark for removal any lines until find the M3
                                if prev.token.startswith('M3'):
                                    found_m3 = True
                                    prev.type = Commands.REMOVE
                                if not found_m3:
                                    prev.type = Commands.REMOVE
                                try:
                                    if prev.active_g_modal_groups[1] != 'G0' and found_m3:
                                        break
                                    elif prev.active_g_modal_groups[1] == 'G0':
                                        prev.type = Commands.REMOVE
                                except KeyError:
                                    # access to the dictionary index failed,
                                    # so no longer in a g0 mode
                                    break
                            j = i+1
                            for j in range(j, len(self._parsed)):
                                next = self._parsed[j]
                                # mark all lines for removal until find M5
                                next.type = Commands.REMOVE
                                if next.token.startswith('M5'):
                                    next.type = Commands.REMOVE
                                    break
                            
                        elif (diameter <= self.active_thickness * thickness_ratio) or \
                           (diameter <= max_hole_size / UNITS_PER_MM):
                            # Only build the hole of within a certain size of
                            # Params:
                            # x:              Hole Centre X position
                            # y:              Hole Centre y position
                            # d:              Hole diameter
                            # kerf:           Kerf width for cut
                            # leadin_radius:  Radius for the lead in arc
                            # splits[]:       List of length segments. Segments will support different speeds. +ve is left of 12 o'clock
                            #                 -ve is right of 12 o'clock
                            #                 and starting positions of the circle. Including overburn
                            if leadin_radius == 0:
                                this_hole_leadin_radius = radius-(radius/4)-(kerf_width/2)
                            else:
                                this_hole_leadin_radius = leadin_radius
                                
                            arc1_distance = circumferance - arc2_distance - torch_off_distance_before_zero
                            arc2_from_zero = arc1_distance + arc2_distance
                            arc3_from_zero = arc2_from_zero + arc3_distance - circumferance
                            line.hole_builder.\
                                plasma_hole(line, centre_x, centre_y, diameter, \
                                            kerf_width, this_hole_leadin_radius, \
                                            [arc1_distance, \
                                             arc2_from_zero, \
                                             arc3_from_zero])
                            
                            # scan forward and back to mark the M3 and M5 as Coammands.REMOVE
                            j = i-1
                            found_m3 = False
                            for j in range(j, -1, -1):
                                prev = self._parsed[j]
                                # mark for removal any lines until find the M3
                                if prev.token.startswith('M3'):
                                    found_m3 = True
                                    prev.type = Commands.REMOVE
                                if not found_m3:
                                    prev.type = Commands.REMOVE
                                try:
                                    if prev.active_g_modal_groups[1] != 'G0' and found_m3:
                                        break
                                    elif prev.active_g_modal_groups[1] == 'G0':
                                        prev.type = Commands.REMOVE
                                except KeyError:
                                    # access to the dictionary index failed,
                                    # so no longer in a g0 mode
                                    break
                            j = i+1
                            for j in range(j, len(self._parsed)):
                                next = self._parsed[j]
                                # mark all lines for removal until find M5
                                next.type = Commands.REMOVE
                                if next.token.startswith('M5'):
                                    next.type = Commands.REMOVE
                                    break
                        else:
                            line.is_hole = False
                            line.hole_builder = None
            i += 1

    def parse(self):
        # setup any global default modal groups that we need to be aware of
        self.set_active_g_modal('G91.1')
        self.set_active_g_modal('G40')
        # start parsing through the loaded file
        for line in self._orig_gcode:
            self._line_num += 1
            self._line = line.strip()
            l = CodeLine(self._line, parent=self)
            try:
                gcode = f'{l.command[0]}{l.command[1]}'
            except:
                gcode = ''
            self.set_active_g_modal(l.token)
            l.save_g_modal_group(self.active_g_modal_grps)
            self._parsed.append(l)



    def dump_parsed(self):
        LOG.debug('Dum parsed gcode to stdio')
        for l in self._parsed:
            #print(f'{l.type}\t\t -- {l.command} \
            #    {l.params} {l.comment}')
            # build up line to go to stdout
            if l.is_hole:
                print('(---- Smart Hole Start ----)')
                l.hole_builder.generate_hole_gcode()
                print('(---- Smart Hole End ----)')
                print()
                continue
            if l.type is Commands.COMMENT:
                out = l.comment
            elif l.type is Commands.OTHER:
                # Other at the moment means not recognised
                out = "; >>  "+l.raw
            elif l.type is Commands.PASSTHROUGH:
                out = l.raw
            elif l.type is Commands.REMOVE:
                # skip line as not to be used
                out = ''
                continue
            else:
                try:
                    out = f"{l.command[0]}{l.command[1]}"
                except:
                    out = ''
                try:
                    for p in l.params:
                        out += f' {p}{l.params[p]}'
                    out += f' {l.comment}'
                    out = out.strip()
                except:
                    out = ''
            print(out, file=sys.stdout)
            sys.stdout.flush()

    def set_ui_hal_cutchart_pin(self):
        if self.active_cutchart is not None:
            rtn = hal.set_p("qtpyvcp.cutchart-id", f"{self.active_cutchart}")
            LOG.debug('Set hal cutchart-id pin')
        else:
            LOG.debug('No active cutchart')


def main():
    global PLASMADB

    try:
        inCode = sys.argv[1]
    except:
        # no arg found, probably being run from command line and someone forgot a file
        print(__doc__)
        return

    if len(inCode) == 0 or '-h' == inCode:
        print(__doc__)
        return

    custom_config_yaml_file_name = normalizePath(path='custom_config.yml', base=os.getenv('CONFIG_DIR', '~/'))
    cfg_dic = load_config_files(custom_config_yaml_file_name)
    
    # we assume that things are sqlit unless we find custom_config.yml
    # pointing to different type of DB
    try:
        db_connect_str = cfg_dic['data_plugins']['plasmaprocesses']['kwargs']['connect_string']
        # if no error then we found a db connection string. Use it.
        PLASMADB = PlasmaProcesses(connect_string=db_connect_str)
    except:
        # no connect string found OR can't connect so assume sqlite on local machine
        PLASMADB = PlasmaProcesses(db_type='sqlite')

    # Start cycling through each line of the file and processing it
    p = PreProcessor(inCode)
    p.parse()
    # Holes processing
    try:
        do_holes = hal.get_value('qtpyvcp.plasma-hole-detect-enable.checked')
    except:
        do_holes = False
    if do_holes:
        p.flag_holes()
    # pass file to stdio and set any hal pins
    p.dump_parsed()
    # Set hal pin on UI for cutchart.id
    p.set_ui_hal_cutchart_pin()
    # Close out DB
    PLASMADB.terminate()


if __name__ == '__main__':
    main()
