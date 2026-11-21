"""Bounded, pitch-preserving PCM pull adapter using Rubber Band's public C API.

No decoding on the ESP, SBC frame drops, or sample-rate/pitch manipulation.
The library is optional for group 0; adaptive groups require Rubber Band >=3.
"""
import array
import ctypes as C
from ctypes.util import find_library
import sys
from .sources import SourceError

class TempoPCM:
    def __init__(self, read, speed):
        self.read, self.speed = read, speed
        try:
            self.lib = C.CDLL(find_library('rubberband') or 'librubberband.so.2')
            fp = C.POINTER(C.c_float)
            self.ptrs = C.POINTER(fp)
            for name, result, args in (
                ('new', C.c_void_p, [C.c_uint,C.c_uint,C.c_int,C.c_double,C.c_double]),
                ('delete',None,[C.c_void_p]),
                ('set_time_ratio',None,[C.c_void_p,C.c_double]),
                ('set_max_process_size',None,[C.c_void_p,C.c_uint]),
                ('get_preferred_start_pad',C.c_uint,[C.c_void_p]),
                ('get_start_delay',C.c_uint,[C.c_void_p]),
                ('get_engine_version',C.c_int,[C.c_void_p]),
                ('available',C.c_int,[C.c_void_p]),
                ('process',None,[C.c_void_p,self.ptrs,C.c_uint,C.c_int]),
                ('retrieve',C.c_uint,[C.c_void_p,self.ptrs,C.c_uint]),
            ):
                fn = getattr(self.lib,'rubberband_'+name); fn.restype=result; fn.argtypes=args
        except (OSError,AttributeError) as exc:
            raise SourceError('Adaptive buffering requires the Rubber Band >=3 runtime library') from exc
        # R3, short window, phase coherent stereo, synchronous bounded processing.
        self.state = self.lib.rubberband_new(44100,2,0x1|0x10000|0x100000|0x10000000|0x20000000,1.,1.)
        if not self.state: raise SourceError('Cannot allocate tempo processor')
        if self.lib.rubberband_get_engine_version(self.state) != 3:
            self.close()
            raise SourceError("Adaptive buffering requires Rubber Band R3")
        self.input_position = 0
        self.input_frames = self.output_frames = 0
        self.lib.rubberband_set_max_process_size(self.state,1024)
        self.discard = self.lib.rubberband_get_start_delay(self.state)
        pad = self.lib.rubberband_get_preferred_start_pad(self.state)
        while pad:
            n=min(1024,pad); self._process(bytes(n*4)); pad-=n

    def _process(self, raw):
        samples=array.array('h',raw)
        if sys.byteorder != 'little': samples.byteswap()
        n=len(samples)//2
        left=(C.c_float*n)(*(v/32768 for v in samples[::2]))
        right=(C.c_float*n)(*(v/32768 for v in samples[1::2]))
        pointers=(C.POINTER(C.c_float)*2)(left,right)
        self.lib.rubberband_process(self.state,pointers,n,0)

    async def read_pcm(self, frames, position=None):
        output=array.array('h')
        # Each read is bounded; source timeout/cancellation propagate unchanged.
        for _ in range(32):
            available=self.lib.rubberband_available(self.state)
            if available > 0:
                n=min(available,1024,frames-len(output)//2 if not self.discard else self.discard)
                left=(C.c_float*n)(); right=(C.c_float*n)()
                pointers=(C.POINTER(C.c_float)*2)(left,right)
                got=self.lib.rubberband_retrieve(self.state,pointers,n)
                if got != n: raise SourceError('Incomplete tempo output')
                if self.discard: self.discard-=n
                else:
                    for i in range(n):
                        output.append(max(-32768,min(32767,round(left[i]*32768))))
                        output.append(max(-32768,min(32767,round(right[i]*32768))))
                    if len(output)//2 == frames:
                        self.output_frames+=frames
                        if sys.byteorder != 'little': output.byteswap()
                        return output.tobytes()
                continue
            speed=self.speed()
            if not .97 <= speed <= 1.03: raise SourceError('Invalid tempo ratio')
            self.lib.rubberband_set_time_ratio(self.state,1/speed)
            raw=await self.read(1024,self.input_position)
            self.input_position+=1024; self.input_frames+=1024
            self._process(raw)
        raise SourceError('Tempo processor exceeded bounded lookahead')

    def close(self):
        if self.state:
            self.lib.rubberband_delete(self.state); self.state=None
