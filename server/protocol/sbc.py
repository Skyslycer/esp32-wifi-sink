"""Version-one transport profile: SBC 44.1kHz, stereo, 16 blocks, 8 subbands, bitpool52."""
from dataclasses import dataclass
from uuid import UUID
from .frames import AUDIO, UNSCHEDULED
FRAME_BYTES=116
SAMPLES=128
PROFILE='sbc_44100_stereo_52'
CAPACITY_FRAMES=65536

def validate_frame(data):
    if len(data)!=FRAME_BYTES or data[:3]!=b'\x9c\xb9\x34':
        raise ValueError('incompatible SBC frame')
    crc=0x0f
    for byte in data[1:3]+data[4:12]:
        for bit in range(7,-1,-1):
            carry=((crc>>7)^((byte>>bit)&1))&1
            crc=(crc<<1)&255
            if carry:crc^=0x1d
    if crc!=data[3]:raise ValueError('invalid SBC header CRC')

@dataclass(frozen=True)
class SBCFrame:
    playback_id: UUID
    epoch: int
    sample_position: int
    data: bytes

    def encode(self):
        if not self.playback_id.int or not 1<=self.epoch<=0xffffffff:
            raise ValueError('invalid playback identity')
        if not self.data or len(self.data)%FRAME_BYTES or len(self.data)>8*FRAME_BYTES:
            raise ValueError('invalid SBC packet size')
        count=len(self.data)//FRAME_BYTES*SAMPLES
        if self.sample_position%128 or not 0<=self.sample_position<(1<<64)-count:raise ValueError('sample position overflow')
        for i in range(0,len(self.data),FRAME_BYTES):validate_frame(self.data[i:i+FRAME_BYTES])
        return AUDIO.pack(self.playback_id.bytes,self.epoch,self.sample_position,UNSCHEDULED,count)+self.data

    @classmethod
    def decode(cls,payload):
        if len(payload)<AUDIO.size:raise ValueError('truncated SBC prefix')
        ident,epoch,pos,pts,count=AUDIO.unpack_from(payload)
        result=cls(UUID(bytes=ident),epoch,pos,payload[AUDIO.size:])
        result.encode()
        if pts!=UNSCHEDULED or count!=len(result.data)//FRAME_BYTES*SAMPLES:
            raise ValueError('invalid SBC timeline')
        return result
