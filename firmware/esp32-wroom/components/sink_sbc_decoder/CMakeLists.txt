# Reuse the decoder at the project's pinned IDF revision; do not enable BT for AUX.
if(NOT CONFIG_SINK_OUTPUT_PCM5102)
    idf_component_register()
    return()
endif()
set(decoder "$ENV{IDF_PATH}/components/bt/host/bluedroid/external/sbc/decoder")
set(sources sink_sbc_decoder.c)
foreach(name alloc bitalloc-sbc bitalloc bitstream-decode decoder-oina decoder-private
             decoder-sbc dequant framing-sbc framing oi_codec_version
             synthesis-8-generated synthesis-dct8 synthesis-sbc)
    list(APPEND sources "${decoder}/srce/${name}.c")
endforeach()
idf_component_register(SRCS ${sources} INCLUDE_DIRS "."
                       PRIV_INCLUDE_DIRS compat "${decoder}/include")
