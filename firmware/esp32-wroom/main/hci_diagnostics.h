#pragma once
#include "cJSON.h"
cJSON *sink_hci_diagnostics(void);
void sink_hci_reset_connect_status(void);
unsigned sink_hci_connect_status(void);
