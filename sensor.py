#!/usr/bin/env python

"""
Copyright (c) 2014-2015 Miroslav Stampar (@stamparm)
See the file 'LICENSE' for copying permission
"""

import mmap
import os
import socket
import subprocess
import struct
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True

from core.common import check_sudo
from core.common import load_trails
from core.enums import BLOCK_MARKER
from core.enums import TRAIL
from core.log import create_log_directory
from core.log import log_event
from core.parallel import worker
from core.parallel import write_block
from core.settings import BUFFER_LENGTH
from core.settings import config
from core.settings import ETH_LENGTH
from core.settings import IPPROTO
from core.settings import IPPROTO_LUT
from core.settings import NO_SUCH_NAME_PER_HOUR_THRESHOLD
from core.settings import NO_SUCH_NAME_COUNTERS
from core.settings import REGULAR_SLEEP_TIME
from core.settings import SNAP_LEN
from core.settings import trails
from core.update import update

_buffer = None
_cap = None
_count = 0
_multiprocessing = False
_n = None
_datalink = None

if config.USE_MULTIPROCESSING:
    try:
        import multiprocessing

        if multiprocessing.cpu_count() > 1:
            _multiprocessing = True
    except (ImportError, OSError, NotImplementedError):
        pass

try:
    import pcapy
except ImportError:
    if subprocess.mswindows:
        exit("[!] please install Pcapy (e.g. 'https://breakingcode.wordpress.com/?s=pcapy') and WinPcap (e.g. 'http://www.winpcap.org/install/')")
    else:
        exit("[!] please install Pcapy (e.g. 'apt-get install python-pcapy')")

def _process_packet(packet, sec, usec):
    """
    Processes single (raw) packet
    """

    try:
        if _datalink == pcapy.DLT_LINUX_SLL:
            packet = packet[2:]

        eth_header = struct.unpack("!HH8sH", packet[:ETH_LENGTH])
        eth_protocol = socket.ntohs(eth_header[3])

        if eth_protocol == IPPROTO:  # IP
            ip_header = struct.unpack("!BBHHHBBH4s4s", packet[ETH_LENGTH:ETH_LENGTH + 20])
            ip_length = ip_header[2]
            packet = packet[:ETH_LENGTH + ip_length]  # truncate
            iph_length = (ip_header[0] & 0xF) << 2
            protocol = ip_header[6]
            src_ip = socket.inet_ntoa(ip_header[8])
            dst_ip = socket.inet_ntoa(ip_header[9])

            if protocol == socket.IPPROTO_TCP:  # TCP
                i = iph_length + ETH_LENGTH
                src_port, dst_port, _, _, doff_reserved, flags = struct.unpack("!HHLLBB", packet[i:i+14])

                if flags == 2:  # SYN set (only)
                    if dst_ip in trails[TRAIL.IP]:
                        log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "TCP", TRAIL.IP, dst_ip, trails[TRAIL.IP][dst_ip][0], trails[TRAIL.IP][dst_ip][1]))
                    elif src_ip in trails[TRAIL.IP]:
                        log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "TCP", TRAIL.IP, src_ip, trails[TRAIL.IP][src_ip][0], trails[TRAIL.IP][src_ip][1]))

                if flags & 8 != 0:  # PSH set
                    tcph_length = doff_reserved >> 4
                    h_size = ETH_LENGTH + iph_length + (tcph_length << 2)
                    data = packet[h_size:]

                    if dst_port == 80 and len(data) > 0:
                        index = data.find("\r\n")
                        if index >= 0:
                            line = data[:index]
                            if line.count(' ') == 2 and " HTTP/" in line:
                                path = line.split(' ')[1]
                            else:
                                return
                        else:
                            return

                        index = data.find("\r\nHost:")
                        if index >= 0:
                            index = index + len("\r\nHost:")
                            host = data[index:data.find("\r\n", index)]
                            host = host.strip()
                        else:
                            return

                        path = path.split('?')[0]
                        path = path.rstrip('/')
                        paths = [path]

                        _ = os.path.splitext(paths[-1])
                        if _[1]:
                            paths.append(_[0])

                        if paths[-1].count('/') > 1:
                            paths.append(paths[-1][:paths[-1].rfind('/')])

                        for path in paths:
                            if path and path in trails[TRAIL.URL]:
                                log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "TCP", TRAIL.URL, path, trails[TRAIL.URL][path][0], trails[TRAIL.URL][path][1]))
                                break

                            url = "%s%s" % (host, path)
                            if url in trails[TRAIL.URL]:
                                log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "TCP", TRAIL.URL, url, trails[TRAIL.URL][url][0], trails[TRAIL.URL][url][1]))
                                break

            elif protocol == socket.IPPROTO_UDP:  # UDP
                i = iph_length + ETH_LENGTH
                _ = packet[i:i + 4]
                if len(_) < 4:
                    return

                src_port, dst_port = struct.unpack("!HH", _)

                if src_port != 53:
                    if dst_ip in trails[TRAIL.IP]:
                        log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "UDP", TRAIL.IP, dst_ip, trails[TRAIL.IP][dst_ip][0], trails[TRAIL.IP][dst_ip][1]))
                    elif src_ip in trails[TRAIL.IP]:
                        log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "UDP", TRAIL.IP, src_ip, trails[TRAIL.IP][src_ip][0], trails[TRAIL.IP][src_ip][1]))

                if dst_port == 53 or src_port == 53:
                    h_size = ETH_LENGTH + iph_length + 8
                    data = packet[h_size:]

                    # Reference: http://www.ccs.neu.edu/home/amislove/teaching/cs4700/fall09/handouts/project1-primer.pdf
                    if len(data) > 6:
                        qdcount = struct.unpack("!H", data[4:6])[0]
                        if qdcount > 0:
                            offset = 12
                            query =  ""

                            while len(data) > offset:
                                length = ord(data[offset])
                                if not length:
                                    query = query[:-1]
                                    break
                                query += data[offset + 1:offset + length + 1] + '.'
                                offset += length + 1

                            if ord(data[2]) == 0x01:  # standard query
                                type_, class_ = struct.unpack("!HH", data[offset + 1:offset + 5])

                                # Reference: http://en.wikipedia.org/wiki/List_of_DNS_record_types
                                if type_ != 12 and class_ == 1:  # Type != PTR, Class IN
                                    parts = query.split('.')

                                    for i in xrange(0, len(parts)):
                                        domain = '.'.join(parts[i:])
                                        if domain in trails[TRAIL.DNS]:
                                            if domain == query:
                                                trail = domain
                                            else:
                                                trail = "(%s)%s" % (query[:-len(domain)], domain)
                                            log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "UDP", TRAIL.DNS, trail, trails[TRAIL.DNS][domain][0], trails[TRAIL.DNS][domain][1]))
                                            break

                            elif (ord(data[2]) & 0x80) and (ord(data[3]) == 0x83):  # standard response, recursion available, no such name
                                if query not in NO_SUCH_NAME_COUNTERS or NO_SUCH_NAME_COUNTERS[query][0] != sec / 3600:
                                    NO_SUCH_NAME_COUNTERS[query] = [sec / 3600, 1]
                                else:
                                    NO_SUCH_NAME_COUNTERS[query][1] += 1

                                    if NO_SUCH_NAME_COUNTERS[query][1] > NO_SUCH_NAME_PER_HOUR_THRESHOLD:
                                        log_event((sec, usec, src_ip, src_port, dst_ip, dst_port, "UDP", TRAIL.DNS, query, "suspicious no such name", "(heuristic)"))

            elif protocol in IPPROTO_LUT:  # non-TCP/UDP (e.g. ICMP)
                if dst_ip in trails[TRAIL.IP]:
                    log_event((sec, usec, src_ip, '-', dst_ip, '-', IPPROTO_LUT[protocol], TRAIL.IP, dst_ip, trails[TRAIL.IP][dst_ip][0], trails[TRAIL.IP][dst_ip][1]))
                elif src_ip in trails[TRAIL.IP]:
                    log_event((sec, usec, src_ip, '-', dst_ip, '-', IPPROTO_LUT[protocol], TRAIL.IP, src_ip, trails[TRAIL.IP][src_ip][0], trails[TRAIL.IP][src_ip][1]))

    except Exception, ex:
        print "[x] '%s'" % ex
        print traceback.format_exc()

def init():
    """
    Performs sensor initialization
    """

    global _cap
    global _datalink

    def update_timer():
        _ = update(server=config.SERVER_UPDATE)

        if _:
            trails.clear()
            trails.update(_)
        elif not trails:
            trails.update(load_trails())

        thread = threading.Timer(config.UPDATE_PERIOD, update_timer)
        thread.daemon = True
        thread.start()

    update_timer()

    create_log_directory()

    if check_sudo() is False:
        exit("[x] please run with sudo/Administrator privileges")

    if subprocess.mswindows and (config.MONITOR_INTERFACE or "").lower() == "any":
        exit("[x] virtual interface 'any' is not available on Windows OS")

    if config.MONITOR_INTERFACE not in pcapy.findalldevs():
        print "[x] interface '%s' not found" % config.MONITOR_INTERFACE
        exit("[!] available interfaces: '%s'" % ",".join(pcapy.findalldevs()))

    print "[i] opening interface '%s'" % config.MONITOR_INTERFACE
    try:
        _cap = pcapy.open_live(config.MONITOR_INTERFACE, SNAP_LEN, True, 0)
    except socket.error, ex:
        if "permitted" in str(ex):
            exit("\n[x] please run with sudo/Administrator privileges")
        elif "No such device" in str(ex):
            exit("\n[x] no such device '%s'" % config.MONITOR_INTERFACE)
        else:
            raise

    if config.CAPTURE_FILTER:
        print "[i] setting filter '%s'" % config.CAPTURE_FILTER
        _cap.setfilter(config.CAPTURE_FILTER)

    _datalink = _cap.datalink()
    if _datalink not in (pcapy.DLT_EN10MB, pcapy.DLT_LINUX_SLL):
        exit("[x] datalink type '%s' not supported" % _datalink)

    if _multiprocessing:
        _init_multiprocessing()

    try:
        p = subprocess.Popen("schedtool -n -2 -M 2 -p 10 -a 0x01 %d" % os.getpid(), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = p.communicate()
        if "not found" in stderr:
            print "[!] please install schedtool for better CPU scheduling (e.g. 'sudo apt-get install schedtool')"
    except:
        pass

def _init_multiprocessing():
    """
    Inits worker processes used in multiprocessing mode
    """

    global _buffer
    global _n

    if _multiprocessing:
        print ("[i] creating %d more processes (%d CPU cores detected)" % (multiprocessing.cpu_count() - 1, multiprocessing.cpu_count()))
        _buffer = mmap.mmap(-1, BUFFER_LENGTH)  # http://www.alexonlinux.com/direct-io-in-python
        _n = multiprocessing.Value('i', lock=False)

        for i in xrange(multiprocessing.cpu_count() - 1):
            process = multiprocessing.Process(target=worker, name=str(i), args=(_buffer, _n, i, multiprocessing.cpu_count() - 1, _process_packet))
            process.daemon = True
            process.start()

def monitor():
    """
    Sniffs/monitors given capturing interface
    """

    def packet_handler(header, packet):
        global _count

        try:
            sec, usec = header.getts()
            if _multiprocessing:
                write_block(_buffer, _count, struct.pack("=II", sec, usec) + packet)
                _n.value = _count + 1
            else:
                _process_packet(packet, sec, usec)
            _count += 1
        except socket.timeout:
            pass

    try:
        _cap.loop(-1, packet_handler)
    except KeyboardInterrupt:
        print "\r[x] Ctrl-C pressed"
    finally:
        if _multiprocessing:
            for _ in xrange(multiprocessing.cpu_count() - 1):
                write_block(_buffer, _n.value, "", BLOCK_MARKER.END)
                _n.value = _n.value + 1
            while multiprocessing.active_children():
                time.sleep(REGULAR_SLEEP_TIME)

def main():
    try:
        init()
        monitor()
    except KeyboardInterrupt:
        print "\r[x] stopping (Ctrl-C pressed)"

if __name__ == "__main__":
    try:
        main()
    except Exception, ex:
        print "\r[!] Unhandled exception occurred ('%s')" % ex
        print "\r[x] Please report the following details at 'https://github.com/stamparm/maltrail/issues':\n---\n'%s'\n---" % traceback.format_exc()

    os._exit(0)
