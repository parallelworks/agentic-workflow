#!/bin/bash
# sites/slurm/env.sh — LEGACY example environment for Slurm sites.
# Sourced before build and run. Edit to match your site's module names.
# The submit wrapper injects the per-target env_load commands from sites.yaml;
# is a hand-editable fallback / override for a specific Slurm cluster.
module purge 2>/dev/null || true
module load gcc/12 2>/dev/null || true
module load openmpi/4.1 2>/dev/null || true
