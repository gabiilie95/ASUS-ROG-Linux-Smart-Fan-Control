// Deliberately performs split-locked atomic operations (an atomic add on an int that straddles
// a cache-line boundary), the same thing the game's threads are being trapped for.
// usage: splitlock <threads> <seconds>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <time.h>
static volatile int stop;
static atomic_long total;
static void *worker(void *arg) {
    char *buf = aligned_alloc(64, 256);
    int *p = (int *)(buf + 62);          // bytes 62..65: crosses the 64-byte line
    long n = 0;
    while (!stop) { __sync_fetch_and_add(p, 1); n++; }
    atomic_fetch_add(&total, n);
    return 0;
}
int main(int argc, char **argv) {
    int nt = argc > 1 ? atoi(argv[1]) : 1; double secs = argc > 2 ? atof(argv[2]) : 5;
    pthread_t t[64]; if (nt > 64) nt = 64;
    for (int i = 0; i < nt; i++) pthread_create(&t[i], 0, worker, 0);
    struct timespec ts = { (time_t)secs, (long)((secs - (long)secs) * 1e9) }; nanosleep(&ts, 0);
    stop = 1; for (int i = 0; i < nt; i++) pthread_join(t[i], 0);
    printf("splitlock: %d thread(s), %.0fs, %ld split-locked ops (%.0f/s)\n", nt, secs, (long)total, total / secs);
    return 0;
}
