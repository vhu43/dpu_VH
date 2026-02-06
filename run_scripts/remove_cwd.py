"""Short script to remove experiment from current evolver experiment directory."""

import os
import sys

cwd = os.getcwd()

with open(f"/home/liusynevolab/curr_ev{sys.argv[1]}_exps", "r", encoding="utf8") as f:
    lines = f.readlines()
with open(f"/home/liusynevolab/curr_ev{sys.argv[1]}_exps", "w", encoding="utf8") as f:
    for line in lines:
        if line.strip("\n") != cwd:
            f.write(line)
