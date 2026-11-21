#include "sink_setup.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sink_control.h"
#include "stream_audio.h"
#include "i2s_sbc_audio.h"
#include "sink_sbc_decoder.h"
#include "sbc_transport.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static portMUX_TYPE dma_lock=portMUX_INITIALIZER_UNLOCKED;
static uint64_t dma_completed_frames, dma_time_us;
static bool dma_sent(i2s_chan_handle_t handle,i2s_event_data_t *event,void *context) {
    portENTER_CRITICAL_ISR(&dma_lock);
    dma_completed_frames+=event->size/4;dma_time_us=esp_timer_get_time();
    portEXIT_CRITICAL_ISR(&dma_lock);
    return false;
}
bool sink_output_command(const char *command, const char *request_id) { return false; }
cJSON *sink_output_status(void) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "kind", "i2s_pcm5102");
    uint64_t frames,stamp;char value[24];
    portENTER_CRITICAL(&dma_lock);frames=dma_completed_frames;stamp=dma_time_us;portEXIT_CRITICAL(&dma_lock);
    snprintf(value,sizeof value,"%llu",(unsigned long long)frames);cJSON_AddStringToObject(j,"dma_completed_frames",value);
    snprintf(value,sizeof value,"%llu",(unsigned long long)stamp);cJSON_AddStringToObject(j,"dma_time_us",value);
    cJSON_AddNumberToObject(j,"dma_capacity_frames",512);
    cJSON_AddBoolToObject(j, "ready", true);
    cJSON_AddStringToObject(j, "audio_codec", SINK_SBC_PROFILE);
    cJSON_AddNumberToObject(j, "sbc_decode_failures", i2s_sbc_decode_failures());
    cJSON_AddNumberToObject(j, "sbc_max_decode_us", i2s_sbc_max_decode_us());
    cJSON_AddNumberToObject(j, "sbc_decoded_frames", i2s_sbc_decoded_frames());
    cJSON_AddNumberToObject(j, "sbc_decoder_memory_bytes", sink_sbc_decoder_memory_bytes());
    cJSON_AddNullToObject(j, "physical_dac_detected");
    cJSON_AddBoolToObject(j, "dac_detection_supported", false);
    cJSON_AddNumberToObject(j, "bclk_gpio", sink_setup_bclk());
    cJSON_AddNumberToObject(j, "lrclk_gpio", sink_setup_lrclk());
    cJSON_AddNumberToObject(j, "data_gpio", sink_setup_dout());
    return j;
}

static i2s_chan_handle_t tx;
static void output_task(void *arg) {
    int16_t block[256];
    while (1) {
        if (!i2s_sbc_audio_fill(block))
            memset(block, 0, sizeof(block));
        size_t offset = 0;
        while (offset < sizeof(block)) {
            size_t written = 0;
            ESP_ERROR_CHECK(
                i2s_channel_write(tx, (uint8_t *)block + offset, sizeof(block) - offset, &written, 1000));
            if (!written) {
                ESP_LOGE("sink", "I2S made no progress");
                abort();
            }
            offset += written;
        }
    }
}
void app_main(void) {
    sink_setup_init();
    int b = sink_setup_bclk(), w = sink_setup_lrclk(), d = sink_setup_dout();
    if (!GPIO_IS_VALID_OUTPUT_GPIO(b) || !GPIO_IS_VALID_OUTPUT_GPIO(w) ||
        !GPIO_IS_VALID_OUTPUT_GPIO(d) || b == w || b == d || w == d || (b >= 6 && b <= 11) ||
        (w >= 6 && w <= 11) || (d >= 6 && d <= 11)) {
        ESP_LOGE("sink", "Invalid I2S pin assignment (including flash pins)");
        abort();
    }
    ESP_ERROR_CHECK(nvs_flash_init());
    stream_audio_init();
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    chan.dma_desc_num=4;chan.dma_frame_num=128;
    ESP_ERROR_CHECK(i2s_new_channel(&chan, &tx, NULL));
    i2s_std_config_t cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(44100),
        .slot_cfg =
            I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg =
            {.mclk = I2S_GPIO_UNUSED, .bclk = b, .ws = w, .dout = d, .din = I2S_GPIO_UNUSED},
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &cfg));
    i2s_event_callbacks_t callbacks={.on_sent=dma_sent};
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(tx,&callbacks,NULL));
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
    configASSERT(xTaskCreate(output_task, "i2s_output", 4096, NULL, 5, NULL) == pdPASS);
    ESP_LOGI("sink", "READY output=pcm5102 rate=44100 bits=16 channels=2 BCLK=%d LRCLK=%d DATA=%d",
             b, w, d);
    stream_audio_output_ready(true);
    sink_network_start();
    char line[96];
    size_t n = 0;
    bool overflow = false;
    while (1) {
        int c = getchar();
        if (c == EOF) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (c == '\n' || c == '\r') {
            line[n] = 0;
            if (n && !overflow) {
                if (!strcmp(line, "status"))
                    ESP_LOGI("sink", "output=pcm5102 heap=%lu minimum=%lu",
                             (unsigned long)esp_get_free_heap_size(),
                             (unsigned long)esp_get_minimum_free_heap_size());
                else
                    ESP_LOGW("sink", "Unsupported command for PCM5102 output");
            }
            n = 0;
            overflow = false;
        } else if (n < sizeof(line) - 1)
            line[n++] = c;
        else
            overflow = true;
    }
}
