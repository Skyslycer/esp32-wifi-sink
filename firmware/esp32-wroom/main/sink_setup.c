/* Browser-provisioned configuration lives outside application and Bluetooth NVS. */
#include "sink_setup.h"
#include "esp_partition.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "cJSON.h"
#include "json_validate.h"
#include "lwip/sockets.h"
#include "sdkconfig.h"
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>
/* Settings are provisioned separately from the firmware image. */
static char ssid[33],password[65],server[16]="0.0.0.0";
static uint16_t port=8765;
static int bclk=26,lrclk=25,dout=22;
static bool initialized;
static const char *string(cJSON *j,const char *name) {
    const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,name);
    return cJSON_IsString(v)?v->valuestring:NULL;
}
static bool pin(int value) {
    return GPIO_IS_VALID_OUTPUT_GPIO(value) && !(value>=6 && value<=11);
}
void sink_setup_init(void) {
    if(initialized)return;
    initialized=true;
#ifdef CONFIG_SINK_OUTPUT_PCM5102
    bclk=CONFIG_SINK_I2S_BCLK;lrclk=CONFIG_SINK_I2S_LRCLK;dout=CONFIG_SINK_I2S_DOUT;
#endif
    const esp_partition_t *p=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,0x40,"sink_setup");
    if(!p)return;
    uint8_t header[12];
    if(esp_partition_read(p,0,header,sizeof(header))!=ESP_OK || memcmp(header,"SINKCFG1",8))return;
    uint32_t length=(uint32_t)header[8]|((uint32_t)header[9]<<8)|((uint32_t)header[10]<<16)|((uint32_t)header[11]<<24);
    if(!length || length>2048 || length+sizeof(header)>p->size)return;
    char *raw=calloc(length+1,1);
    if(!raw)return;
    cJSON *j=NULL;
    if(esp_partition_read(p,sizeof(header),raw,length)!=ESP_OK)goto done;
    /* Embedded NUL would make field lengths ambiguous. */
    if(!bounded_json_text((const uint8_t *)raw,length))goto done;
    j=cJSON_ParseWithLengthOpts(raw,length+1,NULL,true);
    if(!j || !cJSON_IsObject(j) || !unique_json_keys(j))goto done;
    const char *s=string(j,"ssid"),*pass=string(j,"password"),*host=string(j,"server");
    cJSON *pt=cJSON_GetObjectItemCaseSensitive(j,"port");
    struct in_addr address;
    if(!s || !strlen(s) || strlen(s)>32 || !pass || strlen(pass)>64 ||
       !host || strlen(host)>15 || inet_pton(AF_INET,host,&address)!=1 ||
       !cJSON_IsNumber(pt) || pt->valuedouble!=pt->valueint || pt->valueint<1 || pt->valueint>65535)goto done;
    cJSON *b=cJSON_GetObjectItemCaseSensitive(j,"bclk"),*w=cJSON_GetObjectItemCaseSensitive(j,"lrclk"),*d=cJSON_GetObjectItemCaseSensitive(j,"dout");
    if(!cJSON_IsNumber(b)||!cJSON_IsNumber(w)||!cJSON_IsNumber(d)||
       b->valuedouble!=b->valueint||w->valuedouble!=w->valueint||d->valuedouble!=d->valueint||
       !pin(b->valueint)||!pin(w->valueint)||!pin(d->valueint)||
       b->valueint==w->valueint||b->valueint==d->valueint||w->valueint==d->valueint)goto done;
    strcpy(ssid,s);strcpy(password,pass);strcpy(server,host);port=pt->valueint;
    bclk=b->valueint;lrclk=w->valueint;dout=d->valueint;
    ESP_LOGI("setup","Loaded provisioned network configuration");
done:
    cJSON_Delete(j);
    memset(raw,0,length);free(raw);
}
const char *sink_setup_ssid(void){return ssid;}
const char *sink_setup_password(void){return password;}
const char *sink_setup_server(void){return server;}
uint16_t sink_setup_port(void){return port;}
int sink_setup_bclk(void){return bclk;}
int sink_setup_lrclk(void){return lrclk;}
int sink_setup_dout(void){return dout;}
