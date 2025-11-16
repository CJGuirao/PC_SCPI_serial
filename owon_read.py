import socket, time, errno, ctypes, struct

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
print "Open socket"
s.connect(('192.168.88.10',3000))
print "Send command"

s.setblocking(0)


#s.send('STARTBIN')
#s.send('STARTBMP')
s.send('STARTMEMDEPTH')

chunk_size = 1024 * 16
rcvd_data = 0
full_data = ''
timeout = 0

print "Read data ",
    
while (timeout<60):
    if(rcvd_data == 0):
        timeout = timeout + 1
    else:
        timeout = 0
    try:
        data_chunk = s.recv(chunk_size)
        rcvd_data = len(data_chunk)
        full_data += data_chunk
        print rcvd_data,",",
    except socket.error, e:
        if e.args[0] == errno.EWOULDBLOCK: 
            rcvd_data = 0
        else:
            print e
            break
    time.sleep(0.1)


print "Close socket"
s.close()

print len(full_data)

print "Write file"
with open("Output.bin", "wb") as text_file:
    text_file.write(full_data)


def read_u32():
    global data, data_p
    temp = 0
    temp = temp + (ord(data[data_p+3])<<24)
    temp = temp + (ord(data[data_p+2])<<16)
    temp = temp + (ord(data[data_p+1])<<8)
    temp = temp + (ord(data[data_p+0]))
    data_p += 4
    return temp
    
def read_32():
    i = read_u32()
    return ctypes.c_long(i).value

def read_16():
    global data, data_p
    temp = 0
    temp = temp + (ord(data[data_p+1])<<8)
    temp = temp + (ord(data[data_p+0]))
    data_p += 2
    return ctypes.c_short(temp).value

def read_f():
    i = read_u32()    
    return struct.unpack('f',struct.pack('I',i))[0]

def read_char():
    global data, data_p
    temp = struct.unpack('b',data[data_p+0])[0]
    data_p += 1
    return temp

def read_string_nullify(l):
    global data, data_p
    temp = data[data_p:data_p+l-1] + ""
    data_p = data_p + l -1
    return temp

def read_string(l):
    global data, data_p
    temp = data[data_p:data_p+l]
    data_p = data_p + l
    return temp

def get_real_timescale(i):
    return [2.0e-9, 5.0e-9,  
	1.0e-8, 2.0e-8, 5.0e-8, # 10 ns
	1.0e-7, 2.0e-7, 5.0e-7, # 100 ns
	1.0e-6, 2.0e-6, 5.0e-6, # 1 us
	1.0e-5, 2.0e-5, 5.0e-5, # 10 us
	1.0e-4, 2.0e-4, 5.0e-4, # 100 us
	1.0e-3, 2.0e-3, 5.0e-3, # 1 ms
	1.0e-2, 2.0e-2, 5.0e-2, # 10 ms
	1.0e-1, 2.0e-1, 5.0e-1, # 100 ms
	1.0e+0, 2.0e+0, 5.0e+0, # 1 s
	1.0e+1, 2.0e+1, 5.0e+1, # 10 s
        1.0e+2 # 100 s
            ][i]
    

def get_real_voltscale(i):
    return [2.0e-2, 5.0e-2, 1.0e-1, 2.0e-1, 5.0e-1, 1.0e+0, 2.0e+0, 5.0e+0, 1.0e+1, 2.0e+1, 5.0e+1, 1.0e+2][i]

def get_real_attenuation(i):
    return [1.0e0, 1.0e1, 1.0e2, 1.0e3][i]

def parse_channel():
    print "Parse Channel"
    channel ={}
    channel['name'] = read_string_nullify(4)
    channel['unknownint'] = read_32()
    channel['datatype'] = read_32()
    channel['unknown4'] = read_string(4)
    channel['samples_count'] = read_u32()
    channel['samples_file'] = read_u32()
    channel['samples3'] = read_u32()
    channel['timediv'] = get_real_timescale(read_u32())
    channel['offsety'] = read_32()
    channel['voltsdiv'] = get_real_voltscale(read_u32());
    channel['attenuation'] = get_real_attenuation(read_u32())
    channel['time_mul'] = read_f()
    channel['frequency'] = read_f()
    channel['period'] = read_f()
    channel['volts_mul'] = read_f()
    channel['data'] = []
    for i in range (0,channel['samples_file']-1):
        if (channel['datatype'] == 2):
            channel['data'].append(read_16())
        else:
            channel['data'].append(read_char())
    return channel

data = full_data
data_p = 0
data_len = len(full_data)
head = {}

if(data[12:12+3] == 'SPB'):
    head['len'] = read_32()
    head['unknown1'] = read_32()
    head['type'] = read_32()

head['model'] = read_string_nullify(7)

head['intsize'] = read_32()

if(head['intsize'] != 0xFFFFFF):
    head['serial'] = read_string_nullify(30)
    head['triggerstatus'] = read_char()
    head['unknownstatus'] = read_char()
    head['unknownvalue1'] = read_u32()
    head['unknownvalue2'] = read_char()
    head['unknown3'] = read_string(8)

head['channels_count'] = 0

while(data_p < data_len-1):
    if(data[data_p:data_p+2]=='CH'):
        head['channels_count'] = head['channels_count'] + 1
        head['channel'+str(head['channels_count'])] = parse_channel()
        v_mul = head['channel'+str(head['channels_count'])]['volts_mul'] / 100.0
        v_ofs = head['channel'+str(head['channels_count'])]['offsety']
        head['channel'+str(head['channels_count'])]['datav'] = [((x - v_ofs) * v_mul) for x in head['channel'+str(head['channels_count'])]['data']]
    data_p = data_p + 1

#print head

import matplotlib.pyplot as plt
#plt.plot(head['channel1']['datav'])
#plt.plot(head['channel2']['datav'])
#plt.show()

import numpy as np
time_mul = head['channel1']['time_mul'] / 25 * 1e-6
sig = head['channel1']['datav']
sig2 = head['channel2']['datav']
t = np.arange(len(sig)) * time_mul

plt.plot(t,sig,t,sig2)
plt.show()

sp = (np.fft.fft(sig) / len(sig))
freq = np.fft.fftfreq(len(sig),d = time_mul)
plt.plot(freq, np.abs(sp))
plt.show()

#matplotlib.pyplot.plot(numpy.angle(numpy.fft.fft(head['channel1']['datav'])))
#matplotlib.pyplot.plot(numpy.angle(numpy.fft.fft(head['channel2']['datav'])))
#matplotlib.pyplot.show()
