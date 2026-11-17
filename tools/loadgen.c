// usage: loadgen cpu|mem <threads> <seconds>
//   cpu: arithmetic that stays inside the core (no memory traffic)
//   mem: copies 128 MB buffers back and forth (maximum memory-bus traffic)
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
static volatile int stop; static const char *mode;
static void *w(void *a) {
    if (mode[0] == 'c') {
        unsigned long y = 88172645463325252UL; volatile unsigned long sink = 0;
        while (!stop) { for (int i = 0; i < 1000000; i++) { y ^= y << 13; y ^= y >> 7; y ^= y << 17; } sink += y; }
    } else {
        size_t n = 128UL << 20; char *s = malloc(n), *d = malloc(n);
        if (!s || !d) return 0;
        memset(s, 1, n); memset(d, 2, n);
        while (!stop) { memcpy(d, s, n); memcpy(s, d, n); }
        free(s); free(d);
    }
    return 0;
}
int main(int argc, char **argv) {
    mode = argc > 1 ? argv[1] : "cpu"; int nt = argc > 2 ? atoi(argv[2]) : 4; double secs = argc > 3 ? atof(argv[3]) : 5;
    pthread_t t[64]; if (nt > 64) nt = 64;
    for (int i = 0; i < nt; i++) pthread_create(&t[i], 0, w, 0);
    struct timespec ts = { (time_t)secs, 0 }; nanosleep(&ts, 0);
    stop = 1; for (int i = 0; i < nt; i++) pthread_join(t[i], 0);
    return 0;
}
