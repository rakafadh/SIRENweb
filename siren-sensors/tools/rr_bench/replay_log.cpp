// Replays a live_bp.py raw log (W and G lines from main_ppg.cpp) through
// RespirationMonitor with the same artefact and finger logic as the firmware,
// and prints RR once per second plus a summary.
//   g++ -O2 -std=c++17 -Ishim -I../../src/ppg replay_log.cpp ../../src/ppg/respiration.cpp -o replay_log
//   ./replay_log 20261128_150551_raw.txt [per-second.csv]
#include "respiration.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <cmath>
#include <vector>

static float blockDrift(const std::vector<uint32_t> &b) {   // as dcDrift() in main_ppg.cpp for one block
    const int n = (int)b.size(), k = 5;
    double h = 0, t = 0;
    for (int i = 0; i < k; i++) h += b[i];
    for (int i = n - k; i < n; i++) t += b[i];
    h /= k; t /= k;
    return h < 1 ? 1.0f : (float)(std::fabs(t - h) / h);
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: replay_log raw.txt [out.csv]\n"); return 1; }
    FILE *f = fopen(argv[1], "r");
    if (!f) { perror(argv[1]); return 1; }
    FILE *csv = argc > 2 ? fopen(argv[2], "w") : nullptr;
    if (csv) fprintf(csv, "t_s,valid,rate,riiv,riav,rifv,note\n");
    RespirationMonitor resp; resp.begin();
    std::vector<uint32_t> block;
    std::vector<float> rates;
    int seconds = 0, valid = 0, slow = 0;
    int notes[7] = {0};   // filling, artefact, few beats, few sources, disagree, fast?, other
    int tracking = 0, fastN = 0;
    uint32_t lastCompute = 0, t0 = 0;
    static char line[8192];
    while (fgets(line, sizeof line, f)) {
        if (line[0] == 'G' && line[1] == ',') { resp.markDiscontinuity(); continue; }
        if (!(line[0] == 'W' && line[1] == ',')) continue;
        char *p = line + 2;
        uint32_t endMs = strtoul(p, &p, 10); p++;
        float fs = strtof(p, &p);
        std::vector<uint32_t> s;
        while (*p == ',') { p++; s.push_back(strtoul(p, &p, 10)); }
        if (s.empty() || fs <= 0) continue;
        if (!t0) t0 = endMs;
        const int n = (int)s.size();
        for (int i = 0; i < n; i++) {
            uint32_t ms = (uint32_t)(endMs - (n - 1 - i) * 1000.0f / fs + 0.5f);
            resp.addSample(s[i], ms);
            block.push_back(s[i]);
            if ((int)block.size() == 25) {             // one firmware block
                double dc = 0; for (auto v : block) dc += v; dc /= 25;
                if (dc < 50000) resp.reset();
                else {
                    bool art = dc > 220000 || blockDrift(block) > 0.08f;
                    resp.setArtefact(art, art ? (uint32_t)(25 * 1000.0f / fs) : 0);
                }
                block.clear();
            }
            if (ms - lastCompute >= 1000) {
                lastCompute = ms;
                RespirationResult r = resp.compute();
                seconds++;
                if (r.valid) {
                    valid++; rates.push_back(r.rate);
                    if (!strcmp(r.note, "slow")) slow++;
                    if (!strcmp(r.note, "tracking")) tracking++;
                    if (!strcmp(r.note, "fast")) fastN++;
                }
                else {
                    const char *k[6] = {"filling", "artefact", "few beats", "few sources", "disagree", "fast?"};
                    int j = 6; for (int q = 0; q < 6; q++) if (!strcmp(r.note, k[q])) j = q;
                    notes[j]++;
                }
                if (csv) fprintf(csv, "%.0f,%d,%.2f,%.2f,%.2f,%.2f,%s\n", (ms - t0) / 1000.0, r.valid ? 1 : 0,
                                 r.rate, r.riiv, r.riav, r.rifv, r.note);
            }
        }
    }
    std::sort(rates.begin(), rates.end());
    auto pct = [&](double q) { return rates.empty() ? NAN : rates[(size_t)(q * (rates.size() - 1))]; };
    printf("%s: %d s, RR valid %.0f%% (slow path %d, tracking %d, fast %d), median %.1f, p10-p90 %.1f-%.1f /min | no output: filling %d, artefact %d, few beats %d, few sources %d, disagree %d, fast? %d\n",
           argv[1], seconds, 100.0 * valid / std::max(seconds, 1), slow, tracking, fastN, pct(0.5), pct(0.1), pct(0.9),
           notes[0], notes[1], notes[2], notes[3], notes[4], notes[5]);
    if (csv) fclose(csv);
    return 0;
}
