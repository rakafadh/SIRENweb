// Synthetic check of RespirationMonitor with Mayer waves (0.1 Hz vessel-tone
// and heart-rate oscillation) and shallow breathing, the case that broke v2.
//   g++ -O2 -std=c++17 -Ishim -I../../src/ppg synth_check.cpp ../../src/ppg/respiration.cpp -o synth_check
//   ./synth_check <breaths/min> [mayer 0-1] [breathing depth, 1 = normal] [heart rate, default 72]
//   ./synth_check 15 1 0.3     # shallow breathing at 15/min with strong Mayer waves
#include "respiration.h"
#include <cstdlib>
#include <cstdio>
#include <random>
// feed synthetic 25 Hz PPG with breathing at rr per min, print compute() every 5 s
int main(int argc,char**argv){
  float rr=atof(argv[1]); float mayer=argc>2?atof(argv[2]):0; float depth=argc>3?atof(argv[3]):1; float hr0=argc>4?atof(argv[4]):72;
  std::mt19937 g(1); std::normal_distribution<float> N(0,1);
  RespirationMonitor r; r.begin(); double ph=0,rp=0,mp=0;
  for(int i=0;i<25*180;i++){ double t=i/25.0; rp+=2*M_PI*rr/60/25; mp+=2*M_PI*0.1/25;
    float hr=hr0+4*depth*sin(rp)+3*mayer*sin(mp); ph+=hr/60/25; ph-=floor(ph);
    float dc=120000*(1+0.008f*depth*sin(rp+0.6)+0.012f*mayer*sin(mp));
    float a=0.012f*(1+0.25f*depth*sin(rp)+0.1f*mayer*sin(mp+1));
    float pul = ph<0.2?0.5*(1-cos(M_PI*ph/0.2)):0.5*(1+cos(M_PI*(ph-0.2)/0.8));
    float v=dc*(1-a*pul)+0.0006f*dc*N(g);
    r.addSample((uint32_t)v,(uint32_t)(t*1000));
    if(i%(25*5)==0 && t>20){ auto o=r.compute(); auto b=r.beatRate(); printf("t=%3.0f valid=%d rate=%5.1f riiv=%5.1f riav=%5.1f rifv=%5.1f note=%s hr=%5.1f max=%4.0f\n",t,o.valid,o.rate,o.riiv,o.riav,o.rifv,o.note,b.valid?b.bpm:-1.0f,o.rateMax);} }
}
