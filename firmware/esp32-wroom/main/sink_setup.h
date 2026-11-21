#pragma once
#include <stdint.h>
void sink_setup_init(void);
const char *sink_setup_ssid(void);
const char *sink_setup_password(void);
const char *sink_setup_server(void);
uint16_t sink_setup_port(void);
int sink_setup_bclk(void);
int sink_setup_lrclk(void);
int sink_setup_dout(void);
