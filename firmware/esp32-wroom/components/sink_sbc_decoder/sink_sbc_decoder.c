#include "sink_sbc_decoder.h"
#include "oi_codec_sbc.h"
#include <string.h>

static OI_CODEC_SBC_DECODER_CONTEXT context;
static OI_CODEC_SBC_CODEC_DATA_STEREO workspace;
_Static_assert(sizeof(OI_INT32)==4 && sizeof(OI_INT16)==2,
               "SBC fixed-point arithmetic requires 32-bit and 16-bit types");

bool sink_sbc_decoder_reset(void) {
    memset(&context, 0, sizeof(context));
    memset(&workspace, 0, sizeof(workspace));
    return OI_CODEC_SBC_DecoderReset(&context, workspace.data, sizeof(workspace),
                                    2, 2, false, false) == OI_OK;
}
bool sink_sbc_decode_frame(const uint8_t *frame, size_t length, int16_t pcm[256]) {
    if (!frame || !pcm || length != 116 || frame[0] != 0x9c ||
        frame[1] != 0xb9 || frame[2] != 52) return false;
    /* The SDK bit reader prefetches beyond the final sample's byte. Keep that
     * lookahead inside an initialized local buffer, including at ring wrap. */
    uint8_t padded[116+4]={0};
    memcpy(padded, frame, length);
    const OI_BYTE *next = padded;
    OI_UINT32 available = length, written = 512;
    OI_STATUS result = OI_CODEC_SBC_DecodeFrame(&context, &next, &available, pcm, &written);
    return result == OI_OK && available == 0 && next == padded + length && written == 512;
}
size_t sink_sbc_decoder_memory_bytes(void) { return sizeof(context) + sizeof(workspace); }
