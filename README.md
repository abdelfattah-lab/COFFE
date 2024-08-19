COFFE
=====

COFFE is a tool to create the circuitry and area, delay and power models for FPGA tiles (logic, RAM or heterogeneous tiles like DSP blocks).

If you make changes to COFFE, run the "tests_top_level.py" script in the "tests" folder to do some basic checks that existing functionality still works.

How to cite:  
Read the citation guide.

# Local Notes

## Minimum version

Please use Python >= 3.6.

## Running in background

If you would like to run in background, a simple command to use would be:
`nohup python -u <script_filename> <...any arguments> &> <out_file> &`
- `<script_filename>`: python script to run:
    - `coffe.py` would be the usual COFFE 2.0 script for a single job.
    - `coffe_drinker.py` would be the COFFE drinker that runs COFFE jobs in parallel according to a COFFE maker record file (please see the `Kratos-explorer` repository).
- `<out_file>`: log file where `stdout` will be redirected to (i.e., terminal messages); normal convention would be `*.out`.

The terminal output of this will show you the parent process' PID.
If you need to terminate all jobs for whatever reason, then you can kill the parent process and all child processes with:
`pkill -P <parent PID>`