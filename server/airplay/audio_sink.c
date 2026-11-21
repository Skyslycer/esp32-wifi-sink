/* Timing bridge for the pinned Shairport Sync 5.6.3 backend ABI.
 * Replaces audio_pipe.c at build time; exported audio_pipe retains build wiring.
 * Original pipe backend copyright James Laird 2013 / Mike Brady 2014--2025.
 * SPDX-License-Identifier: MIT
 *
 * Audio: local stream, 44-byte big-endian header + <=4096 bytes S16_LE stereo.
 * Feedback: local datagrams, 48-byte fixed record; never waits on a network RTT.
 */
#include "audio.h"
#include "common.h"
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

static int audio_fd=-1, feedback_fd=-1;
static char *endpoint;
static char feedback_path[108];
static uint64_t generation, written_frames, stamp, valid_until;
static int64_t rendered_frame;
static unsigned feedback_flags;
static void put(uint8_t *p,uint64_t v,unsigned n){for(unsigned i=0;i<n;i++)p[n-i-1]=(uint8_t)(v>>(8*i));}
static uint64_t get(const uint8_t *p,unsigned n){uint64_t v=0;for(unsigned i=0;i<n;i++)v=(v<<8)|p[i];return v;}
static int write_all(const void *data,size_t n){
 const uint8_t *p=data;
 while(n){ssize_t k=send(audio_fd,p,n,MSG_NOSIGNAL);if(k<0&&errno==EINTR)continue;if(k<=0)return -1;p+=k;n-=k;}
 return 0;
}
static int record(unsigned kind,unsigned flags,const void *data,unsigned n,uint32_t rtp,uint64_t pts){
 uint8_t h[44]={0};memcpy(h,"SAPT",4);h[4]=1;h[5]=kind;put(h+6,flags,2);
 put(h+8,generation,8);put(h+16,written_frames,8);put(h+24,n,4);put(h+28,rtp,4);put(h+32,pts,8);put(h+40,n*4,4);
 if(audio_fd<0 || write_all(h,sizeof h) || (n && write_all(data,n*4)))return -1;
 written_frames+=n;return 0;
}
static void close_bridge(void){
 if(audio_fd>=0)close(audio_fd);
 audio_fd=-1;
 if(feedback_fd>=0)close(feedback_fd);
 feedback_fd=-1;
 if(feedback_path[0])unlink(feedback_path);
 feedback_flags=0;stamp=valid_until=0;
}
static int prepare(void){
 close_bridge();written_frames=0;generation=get_absolute_time_in_ns();
 struct sockaddr_un addr={.sun_family=AF_UNIX};
 if(!endpoint || strlen(endpoint)>=sizeof(addr.sun_path) || snprintf(feedback_path,sizeof feedback_path,"%s.feedback",endpoint)>=(int)sizeof feedback_path)return -1;
 feedback_fd=socket(AF_UNIX,SOCK_DGRAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0);
 if(feedback_fd<0)die("sink feedback socket: %s",strerror(errno));
 strcpy(addr.sun_path,feedback_path);
 if(bind(feedback_fd,(struct sockaddr*)&addr,sizeof addr))die("sink feedback bind: %s",strerror(errno));
 chmod(feedback_path,0600);
 audio_fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);
 if(audio_fd<0){close_bridge();return -1;}
 struct timeval timeout={.tv_sec=2};int buffer=8192;
 setsockopt(audio_fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof timeout);
 setsockopt(audio_fd,SOL_SOCKET,SO_SNDBUF,&buffer,sizeof buffer);
 memset(&addr,0,sizeof addr);addr.sun_family=AF_UNIX;strcpy(addr.sun_path,endpoint);
 if(connect(audio_fd,(struct sockaddr*)&addr,sizeof addr) || record(2,0,NULL,0,0,0))die("sink bridge connection: %s",strerror(errno));
 return 0;
}
static void update_feedback(void){
 uint8_t b[49];
 for(unsigned i=0;i<32 && feedback_fd>=0;i++){
  ssize_t n=recv(feedback_fd,b,sizeof b,MSG_DONTWAIT);
  if(n<0)break;
  if(n!=48 || memcmp(b,"SAPF",4) || get(b+4,2)!=1 || get(b+8,8)!=generation || get(b+40,4)!=44100)continue;
  uint64_t s=get(b+16,8),until=get(b+32,8),now=get_absolute_time_in_ns();
  if(s<stamp || s>now || until<s || until-s>250000000 || now>until)continue;
  int64_t rendered=(int64_t)get(b+24,8);
  if(get(b+6,2)>3 || rendered < -441000 || rendered>(int64_t)written_frames+441000)continue;
  stamp=s;valid_until=until;feedback_flags=get(b+6,2);rendered_frame=(int64_t)get(b+24,8);
 }
}
static int delay(long *frames){
 update_feedback();uint64_t now=get_absolute_time_in_ns();
 if(!(feedback_flags&1) || now>valid_until || now<stamp)return -EBUSY;
 int64_t presented=rendered_frame;
 if(feedback_flags&2)presented+=(int64_t)((now-stamp)*44100/1000000000);
 if(presented>(int64_t)written_frames)presented=written_frames;
 int64_t pending=(int64_t)written_frames-presented;
 if(pending<0 || pending>44100*10)return -EBUSY;
 *frames=(long)pending;return 0;
}
static int play(void *buf,int samples,int type,uint32_t rtp,uint64_t pts){
 if(samples<0)return -1;
 const uint8_t *p=buf;unsigned offset=0;
 while(offset<(unsigned)samples){
  unsigned n=(unsigned)samples-offset;if(n>1024)n=1024;
  if(record(1,type==play_samples_are_timed,p+offset*4,n,rtp+offset,
            type==play_samples_are_timed ? pts+(uint64_t)offset*1000000000/44100 : 0)){
   close_bridge();return -1;
  }
  offset+=n;
 }
 return 0;
}
static void flush(void){
 generation=get_absolute_time_in_ns();written_frames=0;feedback_flags=0;stamp=0;
 if(audio_fd>=0 && record(3,0,NULL,0,0,0))close_bridge();
}
static void stop(void){if(audio_fd>=0 && record(4,0,NULL,0,0,0))close_bridge();}
static int running(void){return audio_fd>=0 ? 0 : -ENODEV;}
static int init(int argc,char **argv){
 (void)argv;
 /* Shairport passes -1 when there are no backend command-line arguments. */
 if(argc>0)die("sink backend accepts configuration-file options only");
 config.audio_backend_buffer_desired_length=1.0;config.audio_backend_latency_offset=0;
 parse_audio_options("sink",(1<<SPS_FORMAT_S16_LE),(1<<SPS_RATE_44100),(1<<2));
 const char *name=NULL;
 if(!config.cfg || !config_lookup_string(config.cfg,"sink.name",&name))die("sink.name is required");
 endpoint=(char*)name;return 0;
}
static int32_t configuration(unsigned channels,unsigned rate,unsigned format){return search_for_suitable_configuration(channels,rate,format,NULL);}
static int configure(int32_t format,char **map){
 (void)map;
 return FORMAT_FROM_ENCODED_FORMAT(format)==SPS_FORMAT_S16_LE && RATE_FROM_ENCODED_FORMAT(format)==44100 && CHANNELS_FROM_ENCODED_FORMAT(format)==2 ? 0 : EINVAL;
}
static void help(void){printf("    Timestamped ESP32 sink bridge v1; configure sink.name.\n");}
audio_output audio_pipe={.name="sink",.help=help,.init=init,.deinit=close_bridge,
 .prepare=prepare,.get_configuration=configuration,.configure=configure,
 .stop=stop,.is_running=running,.flush=flush,.delay=delay,.stats=NULL,.play=play};
