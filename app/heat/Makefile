# Makefile for the MPI heat solver.
#
# Two builds off ONE source (heat.c):
#   make cpu   -> CPU-only MPI build            (any MPI + C compiler)
#   make gpu   -> MPI + OpenACC GPU offload     (NVIDIA HPC SDK: mpicc wrapping nvc)
#
# site-runner's run workflow builds whichever mode a target declares.
# On a CPU-only site, `make gpu` is EXPECTED to fail — that failure is a finding,
# not a bug. See WORKSHOP.md, Exercise 3.

MPICC   ?= mpicc
CFLAGS  ?= -O3
LDLIBS  ?= -lm

# OpenACC flags for the NVIDIA HPC SDK. Older PGI toolchains use the same -acc.
ACCFLAGS ?= -acc -Minfo=accel

.PHONY: all cpu gpu clean

all: cpu

cpu: heat

heat: heat.c
	$(MPICC) $(CFLAGS) -o heat heat.c $(LDLIBS)

gpu: heat_gpu

heat_gpu: heat.c
	$(MPICC) $(CFLAGS) $(ACCFLAGS) -o heat_gpu heat.c $(LDLIBS)

clean:
	rm -f heat heat_gpu
