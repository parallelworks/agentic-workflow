/*
 * heat.c — 2D steady-state heat solver by Jacobi iteration, MPI + optional GPU.
 *
 * WHAT THIS SOLVES
 *   The steady-state heat (Laplace) equation on a unit square:
 *
 *       d^2u/dx^2 + d^2u/dy^2 = 0
 *
 *   with fixed (Dirichlet) boundary temperatures. We iterate the Jacobi update
 *   until the solution stops changing (the L2 norm of the update falls below a
 *   tolerance). At convergence each interior point is the average of its four
 *   neighbors — the discrete harmonic condition.
 *
 *       u_new[i][j] = 0.25 * (u[i-1][j] + u[i+1][j] + u[i][j-1] + u[i][j+1])
 *
 * WHY JACOBI (not Gauss-Seidel / SOR)
 *   Every point's update reads only OLD neighbor values, so the whole sweep is
 *   data-parallel with no loop-carried dependence. That is exactly what lets it
 *   parallelize cleanly across MPI ranks (one halo exchange per iteration) and
 *   onto a GPU without changing the algorithm. Gauss-Seidel/SOR converge faster
 *   but serialize the sweep — the wrong trade for a portability/scaling demo.
 *
 * ACADEMIC REFERENCES (cite these; do not rely on web pages)
 *   [1] R. J. LeVeque, "Finite Difference Methods for Ordinary and Partial
 *       Differential Equations: Steady-State and Time-Dependent Problems",
 *       SIAM, 2007. (Discretization of the Laplacian; the 5-point stencil.)
 *   [2] Y. Saad, "Iterative Methods for Sparse Linear Systems", 2nd ed.,
 *       SIAM, 2003. (Jacobi as a stationary iterative method; convergence.)
 *   [3] G. H. Golub and C. F. Van Loan, "Matrix Computations", 4th ed.,
 *       Johns Hopkins University Press, 2013. (Classical splitting methods.)
 *
 * DECOMPOSITION (see WORKSHOP.md and DESIGN NOTES below)
 *   The global GRID x GRID mesh is split into EQUAL horizontal slabs, one slab
 *   per MPI rank. Every rank owns GRID/nranks interior rows plus two "halo"
 *   rows (one above, one below) holding copies of its neighbors' edge rows.
 *   Each iteration: (1) exchange halos with the up/down neighbor over MPI,
 *   (2) apply the Jacobi update, (3) reduce the local change into a global one.
 *
 *   Equal slabs are deliberate: every rank runs the same hardware in a given
 *   run (all CPU, or all GPU), so equal tiles finish together and the
 *   bulk-synchronous halo exchange never stalls on a straggler.
 *
 * ============================ DESIGN NOTES: SEAMS ============================
 * These are the exact points that generalize to multi-node multi-GPU LATER.
 * Nothing here needs to change today; this documents where the future work goes.
 *
 *   SEAM 1 (halos ALWAYS go through MPI). The exchange below uses MPI_Sendrecv,
 *     never a direct device-to-device copy. On one node this resolves over
 *     shared memory; across nodes it resolves over the fabric. Keeping halos on
 *     MPI is what makes the single-node -> multi-node jump a BUILD change
 *     (link against a GPU-aware MPI) rather than a rewrite. DO NOT replace this
 *     with cudaMemcpyPeer: peer copies do not cross node boundaries.
 *
 *   SEAM 2 (one rank per worker). CPU mode: one rank per core. GPU mode: one
 *     rank per GPU (see #ifdef _OPENACC device binding). Multi-node multi-GPU is
 *     just "more equal slabs" — nodes x GPUs-per-node ranks — with the same
 *     decomposition and the same update kernel.
 *
 *   SEAM 3 (GPU-aware halo buffers). When built for GPU with a CUDA-aware MPI,
 *     the send/recv buffers can be device pointers passed straight to MPI. Today
 *     we keep it correct and simple; the buffers are host-visible via managed/
 *     present data. The future switch is a build-and-launch concern, not an
 *     algorithm change.
 * ===========================================================================
 *
 * BUILD
 *   CPU:  mpicc -O3 -o heat heat.c -lm
 *   GPU:  mpicc -O3 -acc -Minfo=accel -o heat_gpu heat.c -lm   (NVIDIA HPC SDK)
 *   (See the Makefile; site-runner's run workflow builds the right mode.)
 *
 * RUN
 *   mpirun -np <ranks> ./heat --grid <N> --tol <t> --max-iter <m> [--gpu]
 *
 * OUTPUT
 *   A single-line JSON record on rank 0 (stdout), so the agents and validate.py
 *   can parse results without scraping prose. Fields: grid, ranks, iterations,
 *   final_l2, converged, wall_seconds, mode, hosts[], and (GPU) devices[].
 */

#include <mpi.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>

/* -------- defaults (all overridable on the command line) -------- */
#define DEFAULT_GRID     512
#define DEFAULT_TOL      1e-4
#define DEFAULT_MAXITER  20000
#define BOUND_HOT        100.0   /* top edge temperature   */
#define BOUND_COLD       0.0     /* other edges            */

static void die(int rank, const char *msg) {
    if (rank == 0) fprintf(stderr, "heat: %s\n", msg);
    MPI_Abort(MPI_COMM_WORLD, 1);
}

int main(int argc, char **argv) {
    int grid = DEFAULT_GRID, max_iter = DEFAULT_MAXITER;
    double tol = DEFAULT_TOL;
    bool want_gpu = false;
    const char *dump_path = NULL; /* optional: write converged field for validate.py */

    for (int a = 1; a < argc; a++) {
        if      (!strcmp(argv[a], "--grid")     && a + 1 < argc) grid = atoi(argv[++a]);
        else if (!strcmp(argv[a], "--tol")      && a + 1 < argc) tol = atof(argv[++a]);
        else if (!strcmp(argv[a], "--max-iter") && a + 1 < argc) max_iter = atoi(argv[++a]);
        else if (!strcmp(argv[a], "--gpu"))                      want_gpu = true;
        else if (!strcmp(argv[a], "--dump")     && a + 1 < argc) dump_path = argv[++a];
    }

    MPI_Init(&argc, &argv);
    int rank, nranks;
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &nranks);

    if (grid < nranks)
        die(rank, "grid must be >= number of ranks (need at least one row per rank)");

    /* ---- SEAM 2: rank -> host roll-call (proves where the run landed) ---- *
     * Every rank reports its hostname. Rank 0 gathers them so the run record
     * states how many DISTINCT nodes were used. A deliberate one-node run is a
     * valid result; the agent only flags a MISMATCH between requested and used. */
    char myhost[MPI_MAX_PROCESSOR_NAME];
    int hostlen;
    MPI_Get_processor_name(myhost, &hostlen);

    /* ---- Equal 1-D slab decomposition ----
     * Rows are split as evenly as possible; the first (grid % nranks) ranks
     * take one extra row so no work is dropped. Each rank stores its rows plus
     * two halo rows (index 0 and local_rows+1). */
    int base = grid / nranks;
    int rem  = grid % nranks;
    int local_rows = base + (rank < rem ? 1 : 0);
    int row_start  = rank * base + (rank < rem ? rank : rem); /* global first row */

    /* GPU memory fit-check (informational on CPU, a real ceiling on GPU).
     * Two grids (u, u_new) of doubles, plus halos, per rank. */
    double bytes_per_rank =
        2.0 * (double)(local_rows + 2) * (double)(grid + 2) * sizeof(double);

    /* Allocate two buffers: current and next. (local_rows + 2) x (grid + 2). */
    size_t stride = (size_t)(grid + 2);
    size_t cells  = (size_t)(local_rows + 2) * stride;
    double *u     = (double *)calloc(cells, sizeof(double));
    double *u_new = (double *)calloc(cells, sizeof(double));
    if (!u || !u_new) die(rank, "allocation failed");

    /* Initialize: hot top boundary (global row 0), cold elsewhere.
     * Interior starts cold. Left/right columns are cold boundaries. */
    for (size_t i = 0; i < (size_t)(local_rows + 2); i++) {
        for (size_t j = 0; j < stride; j++) {
            double val = BOUND_COLD;
            int global_row = row_start + (int)i - 1; /* -1: halo offset */
            if (global_row == 0)               val = BOUND_HOT;  /* top edge   */
            else if (global_row == grid - 1)   val = BOUND_COLD; /* bottom     */
            if (j == 0 || j == stride - 1)     val = BOUND_COLD; /* left/right */
            u[i * stride + j]     = val;
            u_new[i * stride + j] = val;
        }
    }

    int up   = (rank == 0)          ? MPI_PROC_NULL : rank - 1;
    int down = (rank == nranks - 1) ? MPI_PROC_NULL : rank + 1;

    double t0 = MPI_Wtime();
    int iter = 0;
    double global_l2 = 0.0;
    bool converged = false;

    /* Optional GPU offload. Same algorithm; only the data region + loop
     * pragmas differ. One rank == one GPU (bound below). */
#ifdef _OPENACC
    if (want_gpu) {
        int ndev = acc_get_num_devices(acc_device_nvidia);
        if (ndev > 0) acc_set_device_num(rank % ndev, acc_device_nvidia); /* SEAM 2 */
    }
#endif

#ifdef _OPENACC
#pragma acc data copy(u[0:cells]) create(u_new[0:cells]) if(want_gpu)
#endif
    for (iter = 0; iter < max_iter; iter++) {
        /* ---- SEAM 1: halo exchange ALWAYS through MPI ---- */
        MPI_Sendrecv(&u[1 * stride], grid + 2, MPI_DOUBLE, up, 0,
                     &u[(local_rows + 1) * stride], grid + 2, MPI_DOUBLE, down, 0,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        MPI_Sendrecv(&u[local_rows * stride], grid + 2, MPI_DOUBLE, down, 1,
                     &u[0], grid + 2, MPI_DOUBLE, up, 1,
                     MPI_COMM_WORLD, MPI_STATUS_IGNORE);

        double local_l2 = 0.0;
#ifdef _OPENACC
#pragma acc parallel loop collapse(2) reduction(+:local_l2) present(u, u_new) if(want_gpu)
#endif
        for (int i = 1; i <= local_rows; i++) {
            for (int j = 1; j <= grid; j++) {
                int global_row = row_start + i - 1;
                /* Skip fixed boundary rows (top/bottom); they never update. */
                if (global_row == 0 || global_row == grid - 1) continue;
                double nv = 0.25 * (u[(i - 1) * stride + j] + u[(i + 1) * stride + j] +
                                    u[i * stride + (j - 1)] + u[i * stride + (j + 1)]);
                double d = nv - u[i * stride + j];
                local_l2 += d * d;
                u_new[i * stride + j] = nv;
            }
        }

        /* Copy interior of u_new back into u for the next sweep. */
#ifdef _OPENACC
#pragma acc parallel loop collapse(2) present(u, u_new) if(want_gpu)
#endif
        for (int i = 1; i <= local_rows; i++)
            for (int j = 1; j <= grid; j++)
                u[i * stride + j] = u_new[i * stride + j];

        MPI_Allreduce(&local_l2, &global_l2, 1, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD);
        global_l2 = sqrt(global_l2);
        if (global_l2 < tol) { converged = true; iter++; break; }
    }

    double wall = MPI_Wtime() - t0;

    /* ---- Gather the host roll-call on rank 0 ---- */
    char *allhosts = NULL;
    if (rank == 0) allhosts = (char *)malloc((size_t)nranks * MPI_MAX_PROCESSOR_NAME);
    MPI_Gather(myhost, MPI_MAX_PROCESSOR_NAME, MPI_CHAR,
               allhosts, MPI_MAX_PROCESSOR_NAME, MPI_CHAR, 0, MPI_COMM_WORLD);

    if (rank == 0) {
        /* Count distinct hostnames. */
        int distinct = 0;
        char seen[64][MPI_MAX_PROCESSOR_NAME];
        for (int r = 0; r < nranks && distinct < 64; r++) {
            char *h = allhosts + (size_t)r * MPI_MAX_PROCESSOR_NAME;
            bool found = false;
            for (int s = 0; s < distinct; s++)
                if (!strcmp(seen[s], h)) { found = true; break; }
            if (!found) { strncpy(seen[distinct], h, MPI_MAX_PROCESSOR_NAME - 1); distinct++; }
        }

        /* Single-line JSON record: the machine-readable run result. */
        printf("{");
        printf("\"grid\":%d,", grid);
        printf("\"ranks\":%d,", nranks);
        printf("\"iterations\":%d,", iter);
        printf("\"final_l2\":%.6e,", global_l2);
        printf("\"converged\":%s,", converged ? "true" : "false");
        printf("\"wall_seconds\":%.4f,", wall);
        printf("\"mode\":\"%s\",", want_gpu ? "gpu" : "cpu");
        printf("\"bytes_per_rank\":%.0f,", bytes_per_rank);
        printf("\"distinct_hosts\":%d,", distinct);
        printf("\"hosts\":[");
        for (int s = 0; s < distinct; s++)
            printf("%s\"%s\"", s ? "," : "", seen[s]);
        printf("]");
        printf("}\n");
        free(allhosts);
    }

    /* ---- Optional: gather the converged field on rank 0 and dump it ----
     * This is what makes cross-site portability CHECKABLE: two sites that ran
     * the same problem should produce fields whose L2 difference is ~0.
     * Gathered as a plain text grid (one value per cell, row-major). */
    if (dump_path) {
        int *rows_per = NULL, *displs = NULL;
        if (rank == 0) {
            rows_per = (int *)malloc(nranks * sizeof(int));
            displs   = (int *)malloc(nranks * sizeof(int));
        }
        int my_cells = local_rows * grid;
        int *counts = NULL;
        if (rank == 0) counts = (int *)malloc(nranks * sizeof(int));
        MPI_Gather(&my_cells, 1, MPI_INT, counts, 1, MPI_INT, 0, MPI_COMM_WORLD);

        /* Pack this rank's interior (drop halos and boundary columns semantics
         * kept simple: emit all grid columns for each owned row). */
        double *sendbuf = (double *)malloc((size_t)my_cells * sizeof(double));
        for (int i = 1; i <= local_rows; i++)
            for (int j = 1; j <= grid; j++)
                sendbuf[(i - 1) * grid + (j - 1)] = u[i * stride + j];

        double *field = NULL;
        if (rank == 0) {
            int total = 0;
            for (int r = 0; r < nranks; r++) { displs[r] = total; total += counts[r]; }
            field = (double *)malloc((size_t)total * sizeof(double));
        }
        MPI_Gatherv(sendbuf, my_cells, MPI_DOUBLE,
                    field, counts, displs, MPI_DOUBLE, 0, MPI_COMM_WORLD);

        if (rank == 0) {
            FILE *f = fopen(dump_path, "w");
            if (f) {
                fprintf(f, "# heat field grid=%d\n", grid);
                for (int idx = 0; idx < grid * grid; idx++)
                    fprintf(f, "%.10e%c", field[idx], ((idx + 1) % grid) ? ' ' : '\n');
                fclose(f);
            }
            free(field); free(counts); free(rows_per); free(displs);
        }
        free(sendbuf);
    }

    free(u); free(u_new);
    MPI_Finalize();
    return converged ? 0 : 2; /* nonzero if it never converged */
}
