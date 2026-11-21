"""Streaming stereo S16LE sample-rate conversion via libsamplerate's C API."""
import array
import ctypes as C
from ctypes.util import find_library
import sys
from .sources import SourceError

class SRCData(C.Structure):
    _fields_ = [('data_in',C.POINTER(C.c_float)),('data_out',C.POINTER(C.c_float)),
                ('input_frames',C.c_long),('output_frames',C.c_long),
                ('input_frames_used',C.c_long),('output_frames_gen',C.c_long),
                ('end_of_input',C.c_int),('src_ratio',C.c_double)]

class StereoResampler:
    def __init__(self, rate):
        if rate != 48000: raise ValueError('Unsupported input sample rate')
        self.state = None
        try: self.lib = C.CDLL(find_library('samplerate') or 'libsamplerate.so.0')
        except OSError as exc: raise SourceError('48 kHz WFAS requires libsamplerate') from exc
        self.lib.src_new.argtypes = [C.c_int,C.c_int,C.POINTER(C.c_int)]
        self.lib.src_new.restype = C.c_void_p
        self.lib.src_process.argtypes = [C.c_void_p,C.POINTER(SRCData)]
        self.lib.src_process.restype = C.c_int
        self.lib.src_reset.argtypes = [C.c_void_p]
        self.lib.src_reset.restype = C.c_int
        self.lib.src_delete.argtypes = [C.c_void_p]
        self.lib.src_delete.restype = C.c_void_p
        error = C.c_int()
        # Fast sinc filter keeps state across datagrams and rejects aliasing.
        self.state = self.lib.src_new(2,2,C.byref(error))
        if not self.state: raise SourceError('Cannot allocate WFAS resampler')

    def process(self, raw):
        if not self.state: raise SourceError('WFAS resampler is closed')
        if len(raw)%4 or len(raw)>22000: raise ValueError('Invalid resampler PCM bounds')
        if not raw: return b''
        samples = array.array('h',raw)
        if sys.byteorder!='little': samples.byteswap()
        frames = len(samples)//2
        source = (C.c_float*len(samples))(*(v/32768 for v in samples))
        capacity = frames+256
        output = (C.c_float*(capacity*2))()
        data = SRCData(source,output,frames,capacity,0,0,0,44100/48000)
        if self.lib.src_process(self.state,C.byref(data)) or data.input_frames_used!=frames:
            raise SourceError('WFAS sample-rate conversion failed')
        result = array.array('h',(max(-32768,min(32767,round(output[i]*32768)))
                                 for i in range(data.output_frames_gen*2)))
        if sys.byteorder!='little': result.byteswap()
        return result.tobytes()

    def reset(self):
        if self.state: self.lib.src_reset(self.state)

    def close(self):
        if self.state:
            self.lib.src_delete(self.state)
            self.state = None
