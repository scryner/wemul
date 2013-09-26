import sys
import time
import select
import os
import socket

from optparse import OptionParser

# global variables and methods
VERSION = '0.2.0'
JUSTPRINT = False

# class definitions
class NetemAdjustor:
    def __init__(self, dst_dev):
        self.dst_dev = dst_dev

        self.haveRoot = False
        self.nClass = 0


    def reset(self):
        print('RESET')
        comm = 'tc qdisc del dev %s root handle 1:' % self.dst_dev

        ret = execute(comm)

        if ret is not 0:
            print('failed comm: %s' % comm)

            self.haveRoot = False
            self.nClass = 0

            raise Exception('RESET FAIL')
        else:
            print('RESET SUCCESS')

        self.haveRoot = False
        self.nClass = 0

        # flush iptables
        comm = 'iptables -t mangle --flush'

        ret = execute(comm)

        if ret is not 0:
            print('failed comm: %s' % comm)
            raise Exception('RESET FAIL')


    def _getClassId(self):
        if self.haveRoot:
            self.nClass += 1
            return '1:%d' % self.nClass
        else:
            self.nClass = 1
            return '1:1'


    def adjust(self, host, up_delay_ms, down_delay_ms, up_bandwidth_mbit, down_bandwidth_mbit, loss_rate_str, unparsed_except_list):
        if loss_rate_str is "":
            loss_rate_str = "0"

        print('ADJUSTING HOST: %s' % host)
        print('ADJUSTING UP: delay: %dms / bandwidth: %dMbit' % (up_delay_ms, up_bandwidth_mbit))
        print('ADJUSTING DOWN: delay: %dms / bandwidth: %dMbit' % (down_delay_ms, down_bandwidth_mbit))
        print('ADJUSTING LOSS: %s%%' % loss_rate_str)

        # parsing exception list
        except_list = []

        if up_bandwidth_mbit > down_bandwidth_mbit:
            max_bw = up_bandwidth_mbit
        else:
            max_bw = down_bandwidth_mbit

        if max_bw == 0:
            max_bw = 1000

        for unparsed_ex in unparsed_except_list:
            print('except: %s' % unparsed_ex)

            tokens = unparsed_ex.split('_')

            ex = {}

            try:
                addr = tokens[0]

                if addr.find('/') < 0:
                    addr = '%s/32' % addr 

                ex['addr'] = addr
            except:
                print('failed to parse to exception token: %s' % unparsed_ex)
                continue

            try:
                ex['delay'] = int(tokens[1])
            except:
                ex['delay'] = 0

            try:
                ex['bw'] = int(tokens[2])
            except:
                ex['bw'] = 1000

            if ex['bw'] == 0:
                ex['bw'] = 1000

            if ex['bw'] > max_bw:
                max_bw = ex['bw']

            try:
                ex['loss_str'] = tokens[3]
            except:
                ex['loss_str'] = ''

            except_list.append(ex)

        # adding root
        if not self.haveRoot:
            class_id = self._getClassId()

            r2q_val = float(max_bw * 1024 * 1024) / 8 / 1500 * 1.1
            r2q_val = int(r2q_val)

            comm0 = 'tc qdisc add dev %s handle 1: root htb r2q %d' % (self.dst_dev, r2q_val)

            ret = execute(comm0)

            if ret is not 0:
                print('ADJUSTING FAIL: adding tc root')
                print('failed comm: %s' % comm0)
                return
            else:
                self.haveRoot = True

            # adding exception filter which doesn't need delay
            for ex in except_list:
                class_id = self._getClassId()

                comm = 'tc class add dev %s parent 1: classid %s htb rate %dMbit' % (self.dst_dev, class_id, ex['bw'])

                ret = execute(comm)

                if ret is not 0:
                    print('ADJUSTING FAIL: adding tc class for exception')
                    print('failed comm: %s' % comm)
                    return

                comm = 'tc filter add dev %s parent 1: protocol ip prio 1 u32 match ip src %s match ip dst %s/32 flowid %s' % (self.dst_dev, ex['addr'], host, class_id)

                ret = execute(comm)

                if ret is not 0:
                    print('ADJUSTING FAIL: adding tc src filter for exception')
                    print('failed comm: %s' % comm)
                    continue

                comm = 'tc filter add dev %s parent 1: protocol ip prio 1 u32 match ip dst %s match ip src %s/32 flowid %s' % (self.dst_dev, ex['addr'], host, class_id)

                ret = execute(comm)

                if ret is not 0:
                    print('ADJUSTING FAIL: adding tc src filter for exception')
                    print('failed comm: %s' % comm)
                    continue

                netem_opt = get_netem_opt(ex['delay'], ex['loss_str'])

                if netem_opt != '':
                    comm = 'tc qdisc add dev %s parent %s handle %d netem %s' % (self.dst_dev, class_id, self.nClass, netem_opt)

                    ret = execute(comm)

                    if ret is not 0:
                        print('ADJUSTING FAIL: adding tc netem for exception')
                        print('failed comm: %s' % comm)
                        continue

        # adding class for upstream
        self._adjust(host, True, up_delay_ms, up_bandwidth_mbit, loss_rate_str)
        self._adjust(host, False, down_delay_ms, down_bandwidth_mbit, loss_rate_str)

        print('ADJUSTING SUCCESS')


    def _adjust(self, host, isUpstream, delay_ms, bandwidth_mbit, loss_rate_str):
        class_id = self._getClassId()

        if bandwidth_mbit == 0:
            comm1 = 'tc class add dev %s parent 1: classid %s htb rate 1000Mbit' % (self.dst_dev, class_id)
        else:
            comm1 = 'tc class add dev %s parent 1: classid %s htb rate %dMbit ceil %dMbit burst 15k' % (self.dst_dev, class_id, bandwidth_mbit, bandwidth_mbit)

        ret = execute(comm1)

        if ret is not 0:
            print('failed comm: %s' % comm1)
            raise Exception('ADJUSTING FAIL: adding tc class')

        # adding filter
        if isUpstream:
            match = 'ip src'
        else:
            match = 'ip dst'

        comm2 = 'tc filter add dev %s parent 1: protocol ip prio 1 u32 match %s %s/32 flowid %s' % (self.dst_dev, match, host, class_id)

        ret = execute(comm2)

        if ret is not 0:
            print('failed comm: %s' % comm2)
            raise Exception('ADJUSTING FAIL: adding tc filter to match %s' % match)

        # adding netem
        netem_opt = get_netem_opt(delay_ms, loss_rate_str)

        if netem_opt != '':
            comm3 = 'tc qdisc add dev %s parent %s handle %d netem %s' % (self.dst_dev, class_id, self.nClass, netem_opt)

            ret = execute(comm3)

            if ret is not 0:
                print('failed comm: %s' % comm3)
                raise Exception('ADJUSTING FAIL: adding tc netem')

        # adjusting route table to mangle
        if isUpstream:
            comm = 'iptables -t mangle -A PREROUTING --source %s -j MARK --set-mark %s' % (host, self.nClass)
        else:
            comm = 'iptables -t mangle -A POSTROUTING --destination %s -j ACCEPT' % (host, self.nClass)

        ret = execute(comm)

        if ret is not 0:
            print('failed comm: %s' % comm)
            raise Exception('ADJUSTING FAIL: mangling to routing table')


def get_local_ip_addr():
    ipaddr = ''
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('google.com', 80))
        ipaddr = s.getsockname()[0]
        s.close()
    except:
        pass

    return ipaddr


def get_netem_opt(delay_ms, loss_rate_str):
    netem_opt = ''

    if delay_ms is not 0:
        netem_opt += 'delay %dms' % delay_ms

    if loss_rate_str is not '' and loss_rate_str is not '0':
        netem_opt += ' loss %s%%' % loss_rate_str

    return netem_opt


def execute(comm):
    print('comm: %s' % comm)


    if JUSTPRINT:
        return 0

    return os.system(comm)


def parse_updown(updown, halfIfEqual):
    tokens = updown.split(',')

    up = 0
    down = 0

    try:
        if len(tokens) is 2:
            up = int(tokens[0])
            down = int(tokens[1])
        else:
            up = int(tokens[0])
            if halfIfEqual:
                down = up / 2
                up = down
            else:
                down = up
    except:
        pass

    return up, down


def main():
    parser = OptionParser(usage="usage: %prog [options]", version="%prog " + VERSION)
    parser.add_option("-r", "--reset", action="store_true", dest="reset_flag", default=False,
                      help="Reset to original states")
    parser.add_option("-i", "--interface", action="store", dest="device", default='eth0',
                      help="Interface name")
    parser.add_option("-d", "--delay", action="store", dest="delay_ms", default="0",
                      help="Delay(ms)")
    parser.add_option("-b", "--bandwidth", action="store", dest="bandwidth_mbit", default="0",
                      help="Bandwidth(MBit)")
    parser.add_option("-e", "--excepts", action="store", dest="except_list", default="",
                      help="Exception list (separated by comma)")
    parser.add_option("-n", "--just-print", action="store_true", dest="just_print", default=False,
                      help="Print the commands that would be executed, but do not excute them")
    parser.add_option("-t", "--target", action="store", dest="target", default="",
                      help="Target address (default: local ip to internet)")
    
    (options, args) = parser.parse_args()

    # parsing options
    device = options.device
    up_delay_ms, down_delay_ms = parse_updown(options.delay_ms, True)
    up_bandwidth_mbit, down_bandwidth_mbit = parse_updown(options.bandwidth_mbit, False)
    loss_rate_str = ''
    target = options.target

    global JUSTPRINT
    JUSTPRINT = options.just_print

    adjustor = NetemAdjustor(device)
    try:
        adjustor.reset()
    except:
        pass

    if options.reset_flag is True:
        sys.exit(0)

    new_except_list = []
    if options.except_list is not '':
        tokens = options.except_list.split(',')

        for tok in tokens:
            new_except_list.append(tok)

    # getting local ip
    if target is "":
        target = get_local_ip_addr()

    # adjusting
    try:
        adjustor.adjust(target, up_delay_ms, down_delay_ms, up_bandwidth_mbit, down_bandwidth_mbit, loss_rate_str, new_except_list)
        print("FINISHED")
    except Exception as ex:
        print('Failed to adjust: %s' % ex)
        adjustor.reset()


if __name__ == '__main__':
	main()
