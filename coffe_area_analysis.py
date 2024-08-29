"""
For folder(s) holding past COFFE drinker runs, collate area infomration from all runs, and plot relevant graphs.

Note: you should probably run this on:
- Python >=3.10
- matplotlib >= 3.9
"""

from tools.logging import *

import argparse
import glob, os
import re
import math
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

################
# Argument Setup
################
parser = argparse.ArgumentParser()
parser.add_argument(
    'folders',
    help='All folders (separated with a space) to recursively look for COFFE report files.',
    nargs='+'
)
parser.add_argument(
    '-r',
    '--report_file',
    help='Report file name to look for. Default: report.txt',
    default='report.txt'
)
parser.add_argument(
    '-x',
    '--x_axis',
    required=True,
    help='(Required) x-axis to use. Look under the values of KEYS_ARCH.'
)
parser.add_argument(
    '-o',
    '--output_file',
    help='name of the image file generated. Default: area_analysis',
    default='area_analysis'
)
args = parser.parse_args()

###########
# Constants
###########
HEADER_ARCH = """ARCHITECTURE PARAMETERS:
-------------------------------------------------

"""
KEYS_ARCH = {
    "Number of BLEs per cluster (N)": "N",
    "LUT size (K)": "K",
    "LUT fracturability level": "use_fluts",
    "Number of adder bits per ALM": "adder_bits",
    "Channel width (W)": "W",
    "Wire segment length (L)": "L",
    "Number of cluster inputs (I)": "I",
    "Number of BLE outputs to general routing (Or)": "Or",
    "Number of BLE outputs to local routing (Ofb)": "Ofb",
    "Total number of cluster outputs (N*Or)": "O",
    "Switch block flexibility (Fs)": "Fs",
    "Cluster input flexibility (Fcin)": "Fcin",
    "Cluster output flexibility (Fcout)": "Fcout",
    "Local MUX population (Fclocal)": "Fclocal",
    "LUT input for register selection MUX (Rsel)": "Rsel",
    "LUT input(s) for register feedback MUX(es) (Rfb)": "Rfb",
}

HEADER_AREA_BREAKDOWN = """  TILE AREA CONTRIBUTIONS
  -----------------------
  """
BLOCKS_AREA_BREAKDOWN = ['LUT', 'FF', 'BLE output', 'Local mux', 'Connection block', 'Switch block', 'Non-active']

###########
# Functions
###########
def get_portrait_square(n, portrait=True):
    """
    Returns an approximate (rows, columns) that is as "squarish" as possible that is guaranteed to fit n (i.e., width * height >= n).
    """
    # find "squarish" layout
    side1 = math.floor(math.sqrt(n))
    side2 = math.ceil(n / float(side1))

    max_side, min_side = max(side1, side2), min(side1, side2)
    return (max_side, min_side) if portrait else (min_side, max_side)

def get_all_report_file_paths(folders):
    """
    Search recursively in all folders for report files.
    """
    ret = []
    for folder in folders:
        ret += glob.glob(os.path.join(os.path.abspath(folder), '**', args.report_file), recursive=True)
    
    return ret

def find_section(file_str, header, end='\n\n'):
    return file_str.split(header)[1].split(end)[0]

def parse_arch(arch_str):
    """
    Deciphers architecture parameters.
    """
    ret = {}
    for line in arch_str.split('\n'):
        k, v = line.strip().split(': ')
        if k not in KEYS_ARCH:
            continue

        try:
            v = int(v)
        except:
            try:
                v = float(v)
            except:
                pass

        ret[KEYS_ARCH[k]] = v
    
    return ret

def parse_area_breakdown(area_breakdown_str):
    """
    Deciphers area breakdown.
    """
    ret = {}
    for line in area_breakdown_str.split('\n'):
        line_split = re.split(' +(?=\d)', line.strip())
        if len(line_split) != 3:
            continue

        block, total_area, frac = line_split
        if block not in BLOCKS_AREA_BREAKDOWN:
            continue

        ret[f"{block}_total_area"] = float(total_area)
        ret[f"{block}_frac"] = float(frac.replace('%', '')) / 100
    
    return ret

def parse_report_file(report_file_path):
    ret = None
    with open(report_file_path, 'r') as f:
        file_str = f.read()

        arch_str = find_section(file_str, HEADER_ARCH)
        area_breakdown_str = find_section(file_str, HEADER_AREA_BREAKDOWN)
        ret = {**parse_arch(arch_str), **parse_area_breakdown(area_breakdown_str)}

    return ret

def plot_graphs(df, x_axis_col, out_file_name, bar_width=0.5):
    """
    Plots 
    """

    # Split DataFrame into axis values
    df_dict = df.to_dict(orient='list')
    x_axis = df_dict[x_axis_col]
    total_areas = {}
    fracs = {}
    for key in BLOCKS_AREA_BREAKDOWN:
        total_areas[key] = df_dict[f"{key}_total_area"]
        fracs[key] = df_dict[f"{key}_frac"]

    # Split plot up
    main_fig = plt.figure(constrained_layout=True)
    fig_w, fig_h = 2, 2
    subfig_rows, subfig_cols = 3, 1
    frac_fig_stacked, frac_fig_indiv, total_areas_fig_indiv = main_fig.subfigures(nrows=subfig_rows, ncols=subfig_cols, height_ratios=[1, 1, 1])

    # draw fractions as stacked bar chart
    frac_fig_stacked.suptitle('% of Areas, stacked')
    ax = frac_fig_stacked.add_subplot(111)
    bottom = np.zeros(len(x_axis))
    for label, weights in fracs.items():
        ax.bar(x_axis, weights, bar_width, label=label, bottom=bottom)
        ax.set_xlabel(x_axis_col)
        bottom += np.array(weights, dtype=float)
    ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))

    # draw fractions and total areas as individual subplots
    rows, cols = get_portrait_square(len(BLOCKS_AREA_BREAKDOWN), portrait=False)
    main_fig.set_size_inches(subfig_cols * fig_w * cols, subfig_rows * fig_h * rows)

    for (title, data_dict, fig) in [
        ('% of Areas, individual', fracs, frac_fig_indiv),
        ('Absolute Areas, individual', total_areas, total_areas_fig_indiv)
    ]:
        fig.suptitle(title)
        axes = fig.subplots(nrows=rows, ncols=cols)
        for i, ax in enumerate(axes.flat):
            if i >= len(BLOCKS_AREA_BREAKDOWN):
                fig.delaxes(ax)
                continue

            key = BLOCKS_AREA_BREAKDOWN[i]
            ax.plot(x_axis, data_dict[key])
            ax.set_title(key, fontsize='small')
            ax.set_xlabel(x_axis_col)
    
    # save image
    main_fig.savefig(f'{out_file_name}.png', bbox_inches='tight', dpi=600)

######
# Main
######
log("Got the following report folder(s):")
log_list(args.folders)
report_file_paths = get_all_report_file_paths(args.folders)
log(f"Got {len(report_file_paths)} report file(s).")
df = pd.DataFrame.from_records([parse_report_file(file) for file in report_file_paths])

log(f"Now plotting area analysis graph, with x-axis as {args.x_axis}...")
plot_graphs(df, args.x_axis, args.output_file)
log(f"Success: {args.output_file}.png")