#pragma once
/* Bounded transport primitives, shared with native sanitizer tests. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define SINK_TCP_TX_CAPACITY 4096
#define SINK_TCP_MAX_OUTPUT 3072

typedef struct { uint8_t bytes[SINK_TCP_TX_CAPACITY]; size_t head, used, high_water; } sink_tcp_tx_t;
static inline uint32_t sink_get32(const uint8_t *p) {
    return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3];
}
static inline void sink_put32(uint8_t *p,uint32_t v) {
    p[0]=v>>24;p[1]=v>>16;p[2]=v>>8;p[3]=v;
}
static inline void sink_tx_append(sink_tcp_tx_t *q,const void *data,size_t n) {
    if(!n)return;
    size_t tail=(q->head+q->used)%SINK_TCP_TX_CAPACITY;
    size_t first=n<SINK_TCP_TX_CAPACITY-tail?n:SINK_TCP_TX_CAPACITY-tail;
    memcpy(q->bytes+tail,data,first);memcpy(q->bytes,(const uint8_t*)data+first,n-first);
    q->used+=n;if(q->used>q->high_water)q->high_water=q->used;
}
/* 0=overflow/invalid; 1=enqueued; 2=optional state superseded under pressure. */
static inline unsigned sink_tx_frame(sink_tcp_tx_t *q,uint8_t kind,uint32_t seq,const void *data,size_t n) {
    if(n>SINK_TCP_MAX_OUTPUT || !seq || q->used>SINK_TCP_TX_CAPACITY)return 0;
    bool optional=kind==5 || kind==7;
    if(optional && q->used)return 2;
    if(n+16>SINK_TCP_TX_CAPACITY-q->used)return 0;
    uint8_t h[16]={'W','S','N','K',1,kind,0,0};sink_put32(h+8,seq);sink_put32(h+12,(uint32_t)n);
    sink_tx_append(q,h,sizeof(h));sink_tx_append(q,data,n);return 1;
}
static inline size_t sink_tx_contiguous(const sink_tcp_tx_t *q) {
    return q->used<SINK_TCP_TX_CAPACITY-q->head?q->used:SINK_TCP_TX_CAPACITY-q->head;
}
static inline bool sink_tx_consume(sink_tcp_tx_t *q,size_t n) {
    if(n>q->used)return false;
    q->head=(q->head+n)%SINK_TCP_TX_CAPACITY;q->used-=n;return true;
}
static inline bool sink_tcp_header(const uint8_t h[16],uint32_t previous,uint32_t *length) {
    uint32_t seq=sink_get32(h+8),n=sink_get32(h+12);
    if(memcmp(h,"WSNK",4)||h[4]!=1||h[6]||h[7]||previous==UINT32_MAX||seq!=previous+1||n>2048)return false;
    *length=n;return true;
}
static inline bool sink_reboot_ready(int64_t now,int64_t due,const sink_tcp_tx_t *q) {
    return due && now>=due && !q->used;
}
