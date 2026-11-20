// Host-side benchmark: emulates main_ppg.cpp loop logic around RespirationMonitor
#include "respiration.h"
#include <cstdio>
#include <cstdlib>
#include <random>
static float pulseShape(float ph){ // realistic PPG volume pulse, peak 1 at ph=0.2
  float up=0.2f;
  float v = ph<up ? 0.5f*(1-cosf((float)M_PI*ph/up)) : 0.5f*(1+cosf((float)M_PI*(ph-up)/(1-up)));
  float d=(ph-0.5f)/0.07f; v+=0.12f*expf(-d*d);
  return v;
}
static float ps(float ph){return pulseShape(ph);}
#include <vector>
#include <string>

struct Scenario { const char* name; float fs; float k; float rr1; float rr2; float tSwitch; bool motion; float hr; int seed; };


int main(int argc,char**argv){
  std::vector<Scenario> sc={
    {"25Hz, gain in range",25,1700,15,22,150,true,78,1},
    {"10Hz, gain in range",10,1700,15,22,150,true,78,2},
    {"10Hz, gain must settle",10,3600,15,22,150,true,78,3},
    {"10Hz, slow breathing 8",10,1700,8,12,150,true,70,4},
    {"10Hz, fast breathing 28",10,1700,28,20,150,true,95,5},
    {"25Hz, gain must settle",25,3600,15,22,150,true,78,6},
    {"25Hz, slow breathing 8",25,1700,8,12,150,true,70,7},
    {"25Hz, fast breathing 28",25,1700,28,20,150,true,95,8},
  };
  const int BUF=100, REF=25; const float TEND=260;
  for(auto&s:sc){
    double agg_first=0,agg_valid=0,agg_wrong=0,agg_rmse=0,agg_track=0,agg_rec=0; int nrep=0;
    for(int rep=0;rep<5;rep++){
    std::mt19937 rng(s.seed*100+rep); std::normal_distribution<float> N(0,1);
    RespirationMonitor resp; resp.begin();
    int led=60; double t=0; float phase=0, rphase=0; float mphase=0;
    std::vector<uint32_t> buf(BUF,0);
    // prefill like setup()
    auto sample=[&](double tsec)->uint32_t{
      float fr=(tsec<s.tSwitch? s.rr1: s.rr2)/60.0f;
      float dt=1.0f/s.fs;
      rphase+=2*M_PI*fr*dt;
      float hr=s.hr+4.0f*sinf(rphase); phase+=hr/60.0f*dt; phase-=floorf(phase);
      float place = tsec<3?0: (tsec<5? (tsec-3)/2:1);
      float dc = 2000 + place*s.k*led*(1+0.008f*sinf(rphase+0.6f));
      float a=0.012f*(1+0.25f*sinf(rphase));
      float v = dc*(1 - place*a*pulseShape(phase)) + 0.0006f*dc*N(rng);
      if(s.motion && tsec>200 && tsec<203) v*= (1+0.12f*sinf(2*M_PI*0.9f*tsec)+0.05f*N(rng));
      if(v<0)v=0; if(v>262143)v=262143; return (uint32_t)v;
    };
    double fs=s.fs; uint32_t respLast=0; bool enabled=false;
    struct R{double t; bool valid; float rate;}; std::vector<R> out;
    RespirationResult latest; int cycle=0;
    auto read=[&](uint32_t &ir){ t+=1.0/fs*(1+0.01*N(rng)*0); uint32_t ms=(uint32_t)(t*1000); ir=sample(t); resp.addSample(ir,ms);
#ifdef NEWRR
      if(enabled && ms-respLast>=1000){respLast=ms; latest=resp.compute(); out.push_back({t,latest.valid,latest.rate});}
#endif
    };
    for(int i=0;i<BUF;i++) read(buf[i]);
    while(t<TEND){
      for(int i=REF;i<BUF;i++) buf[i-REF]=buf[i];
      for(int i=BUF-REF;i<BUF;i++) read(buf[i]);
      double sum=0; for(auto x:buf) sum+=x; uint32_t dc=sum/BUF;
      auto drift=[&](const uint32_t*sg,int len){int blk=len>=50?25:len/5; if(blk<3)blk=3; double h=0,tl=0; for(int i=0;i<blk;i++)h+=sg[i]; for(int i=len-blk;i<len;i++)tl+=sg[i]; h/=blk; tl/=blk; if(h<1)return 1.0; return fabs(tl-h)/h;};
      double dr=drift(buf.data(),BUF);
      bool finger=dc>50000, sat=dc>220000, unstable=dr>0.10;
#ifdef NEWRR
      if(!finger){resp.reset(); latest=RespirationResult();}
      else { double bd=drift(buf.data()+BUF-REF,REF); bool art=sat||bd>0.08; resp.setArtefact(art, art?(uint32_t)(REF*1000.0/fs):0);} 
      enabled=finger;
      // proportional gain
      { static bool hold=false; double bs=0; for(int i=BUF-REF;i<BUF;i++) bs+=buf[i]; uint32_t bdc=bs/REF;
        if(hold){hold=false;} else if(bdc>50000 && (bdc>180000||bdc<60000)){ float ratio=120000.0f/bdc; if(ratio<0.5f)ratio=0.5f; if(ratio>2)ratio=2; float nx=led*ratio; if(nx<8)nx=8; if(nx>192)nx=192; int nl=(int)(nx+0.5f); if(nl!=led){led=nl; resp.markDiscontinuity(); hold=true;} } }
#else
      if(!finger||unstable){resp.reset(); latest=RespirationResult();}
      if(++cycle>=5){cycle=0; if(finger&&!unstable){latest=resp.compute();} out.push_back({t,latest.valid,latest.rate});}
      if(finger){ bool ch=false; if(dc>180000&&led>8){led=std::max(8,led-4);ch=true;} else if(dc<60000&&led<192){led=std::min(192,led+4);ch=true;} if(ch){resp.reset(); latest=RespirationResult();} }
#endif
    }
    // metrics
    double first=-1; for(auto&r:out) if(r.valid){first=r.t;break;}
    int nval=0,ntot=0,nwrong=0; double se=0;
    double track=-1;
    for(auto&r:out){
      if(r.t<5) continue;
      bool trans = (r.t>s.tSwitch && r.t<s.tSwitch+35) || (r.t>200 && r.t<203);
      if(!trans){ ntot++; if(r.valid){ nval++; float ref=r.t<s.tSwitch? s.rr1: s.rr2; double e=r.rate-ref; se+=e*e; if(fabs(e)>4) nwrong++; } }
      if(track<0 && r.t>s.tSwitch && r.valid && fabs(r.rate-s.rr2)<2) track=r.t-s.tSwitch;
    }
    double rec=-1; for(auto&r:out) if(r.t>203 && r.valid){rec=r.t-203;break;}
    agg_first+= first-3; agg_valid+= ntot?100.0*nval/ntot:0; agg_wrong+= nval?100.0*nwrong/nval:0; agg_rmse+= nval?sqrt(se/nval):0; agg_track+= track<0?999:track; agg_rec+= rec<0?999:rec; nrep++;
    }
    printf("%-26s first=%5.1fs  valid=%3.0f%%  wrong(>4)=%4.1f%%  rmse=%5.2f  track_change=%6.1fs  after_motion=%6.1fs\n",s.name,agg_first/nrep,agg_valid/nrep,agg_wrong/nrep,agg_rmse/nrep,agg_track/nrep,agg_rec/nrep);
  }
}
