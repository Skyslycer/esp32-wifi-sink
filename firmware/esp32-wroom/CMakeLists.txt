cmake_minimum_required(VERSION 3.16)
include("${CMAKE_CURRENT_LIST_DIR}/../version.cmake")
# Audio/control uses the framed TCP connection; no MQTT component is shipped.
set(EXCLUDE_COMPONENTS mqtt)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(wifi_sink)
if(CONFIG_SINK_OUTPUT_BLUETOOTH)
    target_link_options(${CMAKE_PROJECT_NAME}.elf PRIVATE
        "-Wl,--wrap=esp_vhci_host_send_packet"
        "-Wl,--wrap=esp_vhci_host_register_callback")
endif()
