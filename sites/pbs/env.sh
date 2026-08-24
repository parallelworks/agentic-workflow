#!/bin/bash
# sites/pbs/env.sh — LEGACY example environment for PBS sites.
# Sourced before build and run. Edit to match your site's module names.
# For the GPU (OpenACC) build, this should load the NVIDIA HPC SDK, which
# provides nvc and a CUDA-aware MPI.
module purge 2>/dev/null || true
module load nvhpc/24.3 2>/dev/null || true
