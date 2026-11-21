// The configuration partition is built locally; Wi-Fi credentials never enter an API request.
export const SESSION_KEY='radio-setup-session-v1';
export function selectedBoard(manifest,id) {
 if(manifest?.schema_version!==1||!manifest.boards?.[id]||!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id))throw Error('Choose a board.');
 const board=manifest.boards[id];
 if(board.config_format!=='sinkcfg1')throw Error('Unsupported board configuration format.');
 return board;
}
export function validateSettings(settings,board) {
 if(!board)throw Error('Choose a board.');
 const bytes=value=>new TextEncoder().encode(value).length;
 if(!settings.ssid||bytes(settings.ssid)>32||/[\x00-\x1f]/.test(settings.ssid))throw Error('Enter a Wi-Fi name of 1–32 bytes.');
 if(bytes(settings.password)>64||/[\x00-\x1f]/.test(settings.password))throw Error('The Wi-Fi password must fit in 64 bytes.');
 if(settings.password&&bytes(settings.password)<8)throw Error('Use an open network or a Wi-Fi password of at least 8 bytes.');
 if(bytes(settings.password)===64&&!/^[a-fA-F0-9]{64}$/.test(settings.password))throw Error('A 64-byte Wi-Fi password must be a hexadecimal PSK.');
 const parts=settings.server.split('.');
 if(parts.length!==4||parts.some(v=>!/^\d{1,3}$/.test(v)||Number(v)>255||String(Number(v))!==v)||['0.0.0.0','255.255.255.255'].includes(settings.server))throw Error('Enter the audio server’s LAN IPv4 address.');
 if(!Number.isInteger(settings.port)||settings.port<1||settings.port>65535)throw Error('Enter a TCP port between 1 and 65535.');
 if(!Object.hasOwn(board.outputs,settings.output))throw Error('Choose an output type.');
 const pins=[settings.bclk,settings.lrclk,settings.dout];
 const valid=board.i2s.output_pins;
 if(new Set(pins).size!==3||pins.some(v=>!valid.includes(v)))throw Error('Choose three distinct output GPIOs supported by the selected board.');
 return settings;
}
export function configurationImage(settings,board) {
 validateSettings(settings,board);
 const {ssid,password,server,port,bclk,lrclk,dout}=settings;
 const data=new TextEncoder().encode(JSON.stringify({ssid,password,server,port,bclk,lrclk,dout}));
 if(data.length>2048)throw Error('Configuration is too large.');
 if(data.length+12>board.config_size)throw Error('Configuration exceeds the board’s setup partition.');
 const image=new Uint8Array(board.config_size);image.fill(255);
 image.set(new TextEncoder().encode('SINKCFG1'));
 new DataView(image.buffer).setUint32(8,data.length,true);image.set(data,12);
 return image;
}

export function usableServerIPv4(value) {
 if(typeof value!=='string')return false;
 const parts=value.split('.');
 if(parts.length!==4||parts.some(part=>!/^\d{1,3}$/.test(part)||Number(part)>255||String(Number(part))!==part))return false;
 const [first,second]=parts.map(Number);
 return first>0&&first!==127&&first<224&&!(first===169&&second===254);
}
export function suggestedServerIPv4(advertised,hostname) {
 if(usableServerIPv4(advertised))return advertised;
 if(usableServerIPv4(hostname))return hostname;
 return '';
}
